import os
import secrets

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _abs(path):
    if not path:
        return path
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def _env(*names, default=None):
    """First non-empty value among `names` in the environment.

    Several settings accept two names because the claim-bundle pipeline and
    this app were configured independently before they merged - the pipeline's
    .env uses DB_*, this app's .env.example used REDSHIFT_*. Both keep working.
    """
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


class Config:
    AWS_REGION = os.environ.get("AWS_REGION", "ap-south-1")
    DEFAULT_ROOT_DIR = os.environ.get("DEFAULT_ROOT_DIR", "") or None
    DATABASE_PATH = _abs(os.environ.get("DATABASE_PATH", "var/reviews.db"))
    CACHE_DIR = _abs(os.environ.get("CACHE_DIR", "var/cache"))
    TEXTRACT_CACHE_DIR = os.path.join(CACHE_DIR, "textract")
    PAGES_CACHE_DIR = os.path.join(CACHE_DIR, "pages")
    SECRET_KEY = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(16)
    REVIEWER_NAME = os.environ.get("REVIEWER_NAME", "")

    SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg"}

    # Max Textract analyze_document calls in flight at once, enforced
    # *process-wide* (across every claim being batch-processed
    # concurrently, not per claim) - keep this under your AWS account's
    # Textract TPS quota for AnalyzeDocument with FORMS+TABLES+SIGNATURES+
    # QUERIES, or you'll just trade sequential latency for throttling
    # retries. Raise it once you know your account's actual quota.
    TEXTRACT_MAX_CONCURRENCY = 4

    FUZZY_MATCH_THRESHOLD = 80
    SOUNDEX_TOKEN_OVERLAP_THRESHOLD = 0.6

    # Below this Laplacian variance, a page image is flagged as blurry.
    ENABLE_BLUR_CHECK = True
    BLUR_VARIANCE_THRESHOLD = 100.0
    # Heuristic thresholds for telling a scanned/typed document apart from a
    # photograph: documents tend to be low-saturation with lots of white
    # background; photos have more color variance and less white.
    DOCUMENT_WHITE_RATIO_THRESHOLD = 0.35
    DOCUMENT_SATURATION_THRESHOLD = 40

    # Signature forensics - all heuristic, meant to flag things for a human
    # reviewer to look closer at, not to assert tampering as fact.
    SIGNATURE_HASH_SIZE = 16  # dhash grid size (hash is hash_size^2 bits)
    ENABLE_DUPLICATE_SIGNATURE_CHECK = True
    SIGNATURE_HASH_MATCH_THRESHOLD = 6  # max Hamming distance to call two signature crops "matching"
    SIGNATURE_HASH_STORE_LIMIT = 10  # nearest-neighbor candidates kept per signature, so the match threshold above can be retuned live without reprocessing
    ENABLE_PASTE_CHECK = True
    ELA_QUALITY = 90  # JPEG quality used when re-encoding for error-level analysis
    ELA_MISMATCH_RATIO_THRESHOLD = 1.8  # inside-bbox vs surrounding-ring mean ELA error ratio
    BORDER_EDGE_DENSITY_THRESHOLD = 0.35  # fraction of the ring around a bbox showing a straight edge seam

    # "Too clean" - suspiciously low scan/sensor noise, suggesting a native
    # digital PDF rather than a physical scan or photo. JPEG compression
    # itself suppresses this metric a lot, so keep this low - on real sample
    # data every genuinely scanned/photographed page measured well under 1.5;
    # this default only flags the cleanest tail. Tune on the Settings page
    # once real native-PDF examples are seen.
    ENABLE_TOO_CLEAN_CHECK = True
    NOISE_FLOOR_TOO_CLEAN_THRESHOLD = 0.3

    # Cropped / cut-off page - content touching the image border rather than
    # a natural margin.
    ENABLE_CROPPED_CHECK = True
    CROP_EDGE_MARGIN_THRESHOLD = 6  # px from the edge counted as "touching"
    CROP_EDGE_DENSITY_THRESHOLD = 0.15  # fraction of ink pixels in the edge strip

    # Possible correction / strikethrough - the least reliable check, hedged
    # in the UI and off by default until a reviewer opts in.
    ENABLE_CORRECTION_CHECK = False
    CORRECTION_ZSCORE_THRESHOLD = 3.0
    CORRECTION_HOTSPOT_MIN_COUNT = 1

    # Face detection ("has face" tag) - OpenCV YuNet/Haar, local/CPU-only.
    ENABLE_FACE_CHECK = True
    FACE_CONFIDENCE_THRESHOLD = 0.7  # 0-1 YuNet score; Haar-cascade fallback hits are always 1.0
    FACE_DETECTION_DPI = 200  # DPI used to rasterize PDF pages when first processed (matches pdf2image's own default - tuning this only affects documents processed after the change)

    # ---------------------------------------------------------------- fetch
    # Claim fetching (the vendored claim-bundle pipeline): pull bundles from
    # client S3 into BUNDLES_ROOT, whose subfolders are claim IDs - i.e. it is
    # itself a valid review root folder, which is what makes fetch -> review a
    # hand-off rather than a conversion.
    #
    # BUNDLES_ROOT holds every downloaded PDF/image, so it grows fast. The
    # default keeps it inside the app's gitignored var/ directory; point it at
    # a non-cloud-synced disk if this checkout lives in OneDrive/Dropbox, since
    # a sync client scanning thousands of claim files both slows the fetch and
    # causes the transient PermissionErrors already seen on the page cache.
    BUNDLES_ROOT = _abs(os.environ.get("BUNDLES_ROOT", "var/claim-bundles"))
    PIPELINE_REPORT_DIR = _abs(os.environ.get("PIPELINE_REPORT_DIR", "var/pipeline_reports"))

    # Warehouse holding the claim index (Redshift). Read via queries.py, which
    # accepts either name in each pair.
    REDSHIFT_HOST = _env("REDSHIFT_HOST", "DB_HOST", default="")
    REDSHIFT_PORT = int(_env("REDSHIFT_PORT", "DB_PORT", default="5439"))
    REDSHIFT_DB = _env("REDSHIFT_DB", "REDSHIFT_DBNAME", "DB_NAME", default="")
    REDSHIFT_USER = _env("REDSHIFT_USER", "DB_USER", default="")
    REDSHIFT_PASSWORD = _env("REDSHIFT_PASSWORD", "DB_PASSWORD", default="")
    REDSHIFT_SSLMODE = _env("REDSHIFT_SSLMODE", "DB_SSLMODE", default="require")
    REDSHIFT_CONNECT_TIMEOUT = int(
        _env("REDSHIFT_CONNECT_TIMEOUT", "DB_CONNECT_TIMEOUT", default="15")
    )

    CLAIM_SOURCE_TABLE = os.environ.get("CLAIM_SOURCE_TABLE", "dmart_solution.claim_paid_t")
    CLAIM_TARGET_SCHEMA = os.environ.get("CLAIM_TARGET_SCHEMA", "public")
    S3_SOURCE_BUCKET = os.environ.get("S3_SOURCE_BUCKET", "mumpmjprodpmjayapp")

    # Claims downloaded+extracted concurrently, and S3 objects fetched
    # concurrently within one claim. Downloads are network-bound, so these
    # overlap latency; raise cautiously if S3 throttling appears.
    FETCH_CLAIM_WORKERS = int(os.environ.get("FETCH_CLAIM_WORKERS", "8"))
    FETCH_DOWNLOAD_WORKERS = int(os.environ.get("FETCH_DOWNLOAD_WORKERS", "4"))
    FETCH_INSERT_BATCH_ROWS = int(os.environ.get("FETCH_INSERT_BATCH_ROWS", "500"))
    # Default value of the /fetch form's "latest N claims" box.
    FETCH_DEFAULT_LIMIT = int(os.environ.get("FETCH_DEFAULT_LIMIT", "100"))
    # Whether "Load extracted tables into Redshift" starts ticked.
    FETCH_LOAD_REDSHIFT_DEFAULT = _env(
        "FETCH_LOAD_REDSHIFT_DEFAULT", default="1"
    ).strip().lower() not in {"0", "false", "no", "off"}

    DEFAULT_QUERIES = [
        {"Text": "What is the patient name?", "Alias": "Name"},
        {"Text": "What is the date of birth?", "Alias": "Date of Birth"},
        {"Text": "What is the person's gender?", "Alias": "Gender"},
        {"Text": "What is the patient's age?", "Alias": "Age"},
        {"Text": "What is the date of admission?", "Alias": "Date of Admission"},
        {"Text": "What is the date of discharge?", "Alias": "Date of Discharge"},
        {"Text": "What is the hospital name?", "Alias": "Hospital Name"},
    ]


def init_dirs(config=Config):
    os.makedirs(os.path.dirname(config.DATABASE_PATH), exist_ok=True)
    os.makedirs(config.TEXTRACT_CACHE_DIR, exist_ok=True)
    os.makedirs(config.PAGES_CACHE_DIR, exist_ok=True)
    os.makedirs(config.BUNDLES_ROOT, exist_ok=True)
