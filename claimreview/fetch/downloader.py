"""Claim-bundle downloader, vendored from claim-bundle-extraction-v2/
sync_claim_bundles.py. Kept deliberately close to the original so fixes can
be diffed across the two copies; the CLI entry points were dropped because
the app calls download_bundle()/build_s3_client() directly.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
import base64
import binascii

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

try:
    import psycopg
    from psycopg import _encodings

    # Amazon Redshift can report the database encoding as "UNICODE".
    # Psycopg 3 does not include that alias by default.
    _encodings.py_codecs[b"UNICODE"] = "utf-8"
except ImportError:
    psycopg = None

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


DEFAULT_SOURCE_BUCKET = "mumpmjprodpmjayapp"
DEFAULT_DESTINATION = Path.cwd() / "data" / "claim-bundle"
DEFAULT_EXTENSIONS = {".json", ".pdf", ".png", ".jpg", ".jpeg"}

# Number of S3 objects downloaded concurrently within a single prefix.
DEFAULT_DOWNLOAD_WORKERS = 8


def build_s3_client(max_pool_connections: int = 32) -> Any:
    """Create a boto3 S3 client sized for concurrent downloads.

    A single client is thread-safe and can be shared across all claims; the
    connection pool must be large enough to serve the concurrent GETs.
    """
    config = Config(
        max_pool_connections=max_pool_connections,
        retries={"max_attempts": 5, "mode": "standard"},
    )
    return boto3.client("s3", config=config)

CLAIM_TABLES = [
    "dmart_solution.claim_paid_t",
    "temp_view_claims",
    "dmart_solution.claim_paid_excel_t_08072026",
]


@dataclass(frozen=True)
class S3Uri:
    bucket: str
    key: str

    @classmethod
    def parse(cls, value: str, default_bucket: str | None = None) -> "S3Uri":
        cleaned = value.strip().strip('"').strip("'")

        if cleaned.startswith("s3://"):
            remainder = cleaned[5:]
            bucket, separator, key = remainder.partition("/")
            if not separator or not bucket or not key:
                raise ValueError(f"Invalid S3 URI: {value}")
            return cls(bucket=bucket, key=key.lstrip("/"))

        if default_bucket and cleaned.startswith(default_bucket + "/"):
            return cls(
                bucket=default_bucket,
                key=cleaned[len(default_bucket) + 1 :].lstrip("/"),
            )

        if default_bucket:
            return cls(bucket=default_bucket, key=cleaned.lstrip("/"))

        bucket, separator, key = cleaned.partition("/")
        if not separator or not bucket or not key:
            raise ValueError(f"Cannot determine bucket and key from: {value}")
        return cls(bucket=bucket, key=key.lstrip("/"))

    def uri(self) -> str:
        return f"s3://{self.bucket}/{self.key}"


@dataclass
class DownloadStats:
    source: str
    destination: str
    files_downloaded: int = 0
    files_decoded: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class Manifest:
    registration_id: str
    started_at_utc: str
    completed_at_utc: str | None = None
    status: str = "running"
    error: str | None = None
    skip_reason: str | None = None
    payer_source: str | None = None
    provider_sources: list[str] = field(default_factory=list)
    destination_root: str | None = None
    payer: DownloadStats | None = None
    providers: list[DownloadStats] = field(default_factory=list)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def iter_json_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from iter_json_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from iter_json_strings(item)


def derive_payer_root(preauth_path: str, source_bucket: str) -> S3Uri:
    uri = S3Uri.parse(preauth_path, default_bucket=source_bucket)
    marker = "/preauthorization/"

    if marker not in uri.key:
        raise ValueError(
            "json_object_perauth does not contain '/preauthorization/': "
            f"{uri.uri()}"
        )

    root_key = uri.key.split(marker, 1)[0].rstrip("/") + "/"
    return S3Uri(bucket=uri.bucket, key=root_key)


def provider_roots_from_payload(
    payload: Any,
    source_bucket: str,
    registration_id: str,
) -> set[S3Uri]:
    """Extract provider claim roots from any nested JSON string.

    Expected provider object paths resemble:
      s3://bucket/TMS/provider/<id1>/<id2>/<registration_id>/forms/...

    The returned root ends at <registration_id>/ so downloading it recreates
    beneficary/, claims/, forms/, and preauthorization/ under provider/.
    """

    roots: set[S3Uri] = set()
    escaped_claim_id = re.escape(str(registration_id))

    patterns = [
        re.compile(
            rf"s3://(?P<bucket>[A-Za-z0-9._-]+)/"
            rf"(?P<key>TMS/provider/[^\s\"'<>]+?/{escaped_claim_id}/)",
            re.IGNORECASE,
        ),
        re.compile(
            rf"(?P<key>(?:{re.escape(source_bucket)}/)?"
            rf"TMS/provider/[^\s\"'<>]+?/{escaped_claim_id}/)",
            re.IGNORECASE,
        ),
    ]

    for text in iter_json_strings(payload):
        normalized = text.replace("\\/", "/")

        for pattern in patterns:
            for match in pattern.finditer(normalized):
                bucket = match.groupdict().get("bucket") or source_bucket
                key = match.group("key")

                if key.startswith(bucket + "/"):
                    key = key[len(bucket) + 1 :]

                roots.add(S3Uri(bucket=bucket, key=key.lstrip("/")))

    return roots


def get_json_object(s3_client: Any, uri: S3Uri) -> Any:
    response = s3_client.get_object(Bucket=uri.bucket, Key=uri.key)
    raw = response["Body"].read()

    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError(f"JSON object is not UTF-8: {uri.uri()}") from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in {uri.uri()}: {exc}") from exc



def load_environment(env_file: Path | None) -> Path | None:
    """Load DB and AWS-related settings from a .env file when available."""
    if load_dotenv is None:
        raise RuntimeError(
            "python-dotenv is not installed. Run: python3 -m pip install --user python-dotenv"
        )

    candidates: list[Path] = []
    if env_file is not None:
        candidates.append(env_file.expanduser().resolve())
    else:
        candidates.extend([
            (Path.cwd() / ".env").resolve(),
            (Path(__file__).resolve().parent / ".env").resolve(),
        ])

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return candidate

    if env_file is not None:
        raise FileNotFoundError(f"Environment file not found: {candidates[0]}")

    return None

def fetch_preauth_path_from_db(registration_id: str) -> str:
    if psycopg is None:
        raise RuntimeError(
            "psycopg is not installed. Install psycopg[binary] or pass "
            "--preauth-s3-path explicitly."
        )

    required = ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"]
    missing = [name for name in required if not os.getenv(name)]

    if missing:
        raise RuntimeError(
            "Missing database environment variables: " + ", ".join(missing)
        )

    connection_kwargs: dict[str, Any] = {
        "host": os.environ["DB_HOST"],
        "dbname": os.environ["DB_NAME"],
        "user": os.environ["DB_USER"],
        "password": os.environ["DB_PASSWORD"],
        "port": int(os.getenv("DB_PORT", "5432")),
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "15")),
        "sslmode": os.getenv("DB_SSLMODE", "require"),
    }

    with psycopg.connect(**connection_kwargs) as connection:
        with connection.cursor() as cursor:
            for table in CLAIM_TABLES:
                query = f"""
                    SELECT json_object_perauth
                    FROM {table}
                    WHERE registration_id = %s
                      AND json_object_perauth IS NOT NULL
                      AND json_object_perauth <> ''
                    ORDER BY last_insert_dt DESC NULLS LAST
                    LIMIT 1
                """

                cursor.execute(query, (registration_id,))
                row = cursor.fetchone()

                if row:
                    print(
                        f"Found registration_id={registration_id} "
                        f"in table {table}"
                    )
                    return str(row[0])

    raise LookupError(
        f"No json_object_perauth found for "
        f"registration_id={registration_id} in tables: "
        + ", ".join(CLAIM_TABLES)
    )
     


def is_allowed_file(key: str, allowed_extensions: set[str]) -> bool:
    if not allowed_extensions:
        return True
    return Path(key).suffix.lower() in allowed_extensions


def safe_local_path(base: Path, relative_key: str) -> Path:
    """Build a local path while preventing '..' path traversal."""
    candidate = (base / Path(relative_key)).resolve()
    base_resolved = base.resolve()

    try:
        candidate.relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"Unsafe relative S3 key: {relative_key}") from exc

    return candidate

def decode_s3_object_content(
    content: bytes,
    source_key: str,
) -> tuple[bytes, bool]:
    """
    Decode a Base64-encoded PDF or image retrieved from S3.

    Returns:
        (final_binary_content, was_decoded)
    """

    # Already a normal binary file.
    if content.startswith(b"%PDF-"):
        return content, False

    if content.startswith(b"\xff\xd8\xff"):
        return content, False  # JPEG

    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return content, False  # PNG

    if content.startswith((b"GIF87a", b"GIF89a")):
        return content, False  # GIF

    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return content, False  # WebP

    try:
        text_content = content.decode("utf-8").strip()
    except UnicodeDecodeError:
        # Unknown binary data; leave unchanged.
        return content, False

    # Handles:
    # data:application/pdf;base64,JVBER...
    # data:image/jpeg;base64,/9j/...
    # data:image/png;base64,iVBOR...
    if ";base64," in text_content:
        base64_content = text_content.split(";base64,", 1)[1]
    else:
        # Some objects may contain only raw Base64 text.
        base64_content = text_content

    # Remove spaces and line breaks sometimes present in Base64.
    base64_content = "".join(base64_content.split())

    try:
        decoded_content = base64.b64decode(
            base64_content,
            validate=True,
        )
    except (ValueError, binascii.Error):
        return content, False

    # Only accept decoded output when it is a known PDF/image format.
    if decoded_content.startswith(b"%PDF-"):
        return decoded_content, True

    if decoded_content.startswith(b"\xff\xd8\xff"):
        return decoded_content, True

    if decoded_content.startswith(b"\x89PNG\r\n\x1a\n"):
        return decoded_content, True

    if decoded_content.startswith((b"GIF87a", b"GIF89a")):
        return decoded_content, True

    if (
        decoded_content.startswith(b"RIFF")
        and len(decoded_content) >= 12
        and decoded_content[8:12] == b"WEBP"
    ):
        return decoded_content, True

    # It decoded successfully, but it is not a recognized PDF/image.
    # Preserve the original content to avoid corrupting JSON or other files.
    return content, False

def download_and_decode_object(
    s3_client: Any,
    bucket: str,
    source_key: str,
    destination_path: Path,
    dry_run: bool,
    verbose: bool = True,
) -> dict[str, Any]:
    """
    Read an S3 object, decode Base64 PDF/image content when needed,
    and save the final binary content to EC2.
    """

    result = {
        "source": f"s3://{bucket}/{source_key}",
        "destination": str(destination_path),
        "decoded": False,
        "status": "planned" if dry_run else "pending",
        "error": None,
    }

    if dry_run:
        if verbose:
            print(
                f"DRY-RUN s3://{bucket}/{source_key} "
                f"-> {destination_path}"
            )
        return result

    try:
        response = s3_client.get_object(
            Bucket=bucket,
            Key=source_key,
        )

        source_content = response["Body"].read()

        extension = destination_path.suffix.lower()

        if extension in {
            ".pdf",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".webp",
        }:
            final_content, was_decoded = decode_s3_object_content(
                source_content,
                source_key,
            )
        else:
            final_content = source_content
            was_decoded = False

        destination_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        destination_path.write_bytes(final_content)

        result["decoded"] = was_decoded
        result["status"] = "downloaded"

        if verbose:
            action = "DOWNLOADED + DECODED" if was_decoded else "DOWNLOADED"
            print(
                f"{action} s3://{bucket}/{source_key} "
                f"-> {destination_path}"
            )

        return result

    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"

        if verbose:
            print(
                f"FAILED s3://{bucket}/{source_key}: "
                f"{result['error']}"
            )

        return result

def download_prefix(
    s3_client: Any,
    source: S3Uri,
    destination: Path,
    allowed_extensions: set[str],
    dry_run: bool,
    max_workers: int = DEFAULT_DOWNLOAD_WORKERS,
    verbose: bool = True,
) -> DownloadStats:
    stats = DownloadStats(
        source=source.uri(),
        destination=str(destination),
    )

    paginator = s3_client.get_paginator("list_objects_v2")
    found_any_object = False

    # (source_key, local_path, size) for every object that must be downloaded.
    tasks: list[tuple[str, Path, int]] = []

    try:
        pages = paginator.paginate(Bucket=source.bucket, Prefix=source.key)

        for page in pages:
            for obj in page.get("Contents", []):
                source_key = str(obj["Key"])
                found_any_object = True

                if source_key.endswith("/"):
                    continue

                if not is_allowed_file(source_key, allowed_extensions):
                    stats.files_skipped += 1
                    continue

                relative_key = source_key[len(source.key):].lstrip("/")
                if not relative_key:
                    continue

                local_path = safe_local_path(destination, relative_key)
                tasks.append((source_key, local_path, int(obj.get("Size", 0))))

        if dry_run:
            for source_key, local_path, size in tasks:
                download_and_decode_object(
                    s3_client=s3_client,
                    bucket=source.bucket,
                    source_key=source_key,
                    destination_path=local_path,
                    dry_run=True,
                    verbose=verbose,
                )
                stats.files_downloaded += 1
                stats.bytes_downloaded += size
        elif tasks:
            def _download(task: tuple[str, Path, int]) -> tuple[str, Path, dict[str, Any]]:
                source_key, local_path, _size = task
                file_result = download_and_decode_object(
                    s3_client=s3_client,
                    bucket=source.bucket,
                    source_key=source_key,
                    destination_path=local_path,
                    dry_run=False,
                    verbose=verbose,
                )
                return source_key, local_path, file_result

            workers = max(1, min(max_workers, len(tasks)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(_download, tasks))

            for source_key, local_path, file_result in results:
                if file_result["status"] == "downloaded":
                    stats.files_downloaded += 1

                    if file_result["decoded"]:
                        stats.files_decoded += 1

                    try:
                        stats.bytes_downloaded += local_path.stat().st_size
                    except OSError:
                        pass
                else:
                    stats.files_failed += 1
                    message = (
                        f"Failed processing s3://{source.bucket}/{source_key}: "
                        f"{file_result['error']}"
                    )
                    stats.errors.append(message)
                    if verbose:
                        print(f"ERROR: {message}", file=sys.stderr)

        if not found_any_object:
            stats.errors.append(f"No S3 objects found under {source.uri()}")

    except (ClientError, BotoCoreError) as exc:
        stats.errors.append(
            f"Failed listing {source.uri()}: {type(exc).__name__}: {exc}"
        )

    return stats

def write_manifest(path: Path, manifest: Manifest) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(asdict(manifest), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temporary_path.replace(path)

def normalize_client_s3_path(path: str, source_bucket: str) -> str:
    uri = S3Uri.parse(path, default_bucket=source_bucket)

    if uri.key.startswith("payer/") or uri.key.startswith("provider/"):
        uri = S3Uri(
            bucket=uri.bucket,
            key="TMS/" + uri.key,
        )

    return uri.uri()

def s3_object_exists(s3_client: Any, uri: S3Uri) -> bool:
    try:
        s3_client.head_object(
            Bucket=uri.bucket,
            Key=uri.key,
        )
        return True

    except ClientError as exc:
        error_code = str(
            exc.response.get("Error", {}).get("Code", "")
        )

        if error_code in {"404", "NoSuchKey", "NotFound"}:
            return False

        raise

def provider_roots_from_local_payer_jsons(
    payer_root: Path,
    source_bucket: str,
    registration_id: str,
) -> set[S3Uri]:
    roots: set[S3Uri] = set()

    for json_path in payer_root.rglob("*.json"):
        if json_path.name == "manifest.json":
            continue

        try:
            payload = json.loads(
                json_path.read_text(encoding="utf-8-sig")
            )
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            continue

        roots.update(
            provider_roots_from_payload(
                payload=payload,
                source_bucket=source_bucket,
                registration_id=registration_id,
            )
        )

    return roots


def download_bundle(
    registration_id: str,
    *,
    preauth_path: str | None = None,
    s3_client: Any = None,
    destination: Path = DEFAULT_DESTINATION,
    source_bucket: str = DEFAULT_SOURCE_BUCKET,
    allowed_extensions: set[str] | None = None,
    dry_run: bool = False,
    download_workers: int = DEFAULT_DOWNLOAD_WORKERS,
    verbose: bool = True,
) -> Manifest:
    """Download one payer/provider claim bundle and write manifest.json.

    Importable entry point used by the daily pipeline. Pass ``preauth_path``
    (the ``json_object_perauth`` value already known to the caller) to skip the
    per-claim Redshift lookup, and pass a shared ``s3_client`` to reuse one
    thread-safe boto3 client/connection-pool across many claims.

    Returns the completed Manifest (already persisted to disk).
    """
    registration_id = str(registration_id).strip()
    destination_root = Path(destination).expanduser().resolve() / registration_id
    manifest_path = destination_root / "manifest.json"

    if allowed_extensions is None:
        allowed_extensions = set(DEFAULT_EXTENSIONS)

    manifest = Manifest(
        registration_id=registration_id,
        started_at_utc=utc_now(),
        destination_root=str(destination_root),
    )

    # Write an initial manifest immediately so unexpected failures are visible.
    write_manifest(manifest_path, manifest)

    def log(message: str, *, err: bool = False) -> None:
        if verbose:
            print(message, file=sys.stderr if err else sys.stdout)

    log("Started")

    if s3_client is None:
        s3_client = build_s3_client()

    try:
        if preauth_path is None:
            preauth_path = fetch_preauth_path_from_db(registration_id)
        preauth_path = normalize_client_s3_path(preauth_path, source_bucket)
        preauth_uri = S3Uri.parse(preauth_path, default_bucket=source_bucket)
        payer_root = derive_payer_root(preauth_path, source_bucket)

        manifest.payer_source = payer_root.uri()
        write_manifest(manifest_path, manifest)

        log(f"mode: {'DRY-RUN' if dry_run else 'EXECUTE'}")
        log(f"registration_id: {registration_id}")
        log(f"preauthorization JSON: {preauth_uri.uri()}")
        log(f"payer root: {payer_root.uri()}")
        log(f"destination root: {destination_root}")

        provider_roots: set[S3Uri] = set()

        # First attempt: inspect the exact preauthorization JSON from Redshift.
        if s3_object_exists(s3_client, preauth_uri):
            preauth_payload = get_json_object(s3_client, preauth_uri)
            log("Payer files exist.")
            provider_roots.update(
                provider_roots_from_payload(
                    payload=preauth_payload,
                    source_bucket=source_bucket,
                    registration_id=registration_id,
                )
            )
        else:
            log(
                "WARNING: Redshift-linked preauthorization JSON does not "
                f"exist in S3: {preauth_uri.uri()}"
            )

        # Download payer bundle regardless
        manifest.payer = download_prefix(
            s3_client=s3_client,
            source=payer_root,
            destination=destination_root / "payer",
            allowed_extensions=allowed_extensions,
            dry_run=dry_run,
            max_workers=download_workers,
            verbose=verbose,
        )

        # Fallback: scan all downloaded payer JSON files
        if not provider_roots and not dry_run:
            provider_roots = provider_roots_from_local_payer_jsons(
                payer_root=destination_root / "payer",
                source_bucket=source_bucket,
                registration_id=registration_id,
            )

        manifest.provider_sources = [
            root.uri() for root in sorted(provider_roots, key=lambda item: item.key)
        ]

        if not provider_roots:
            payer_downloaded = (
                manifest.payer is not None
                and manifest.payer.files_downloaded > 0
            )

            if payer_downloaded:
                manifest.status = "partially_completed"
                manifest.error = None
                manifest.skip_reason = (
                    "Payer bundle downloaded, but no provider S3 root "
                    "was found in any payer JSON file."
                )
                log(f"PARTIAL: {manifest.skip_reason}")
                return manifest

            manifest.status = "failed"
            manifest.error = (
                "No provider S3 root was found and no payer files "
                "were downloaded successfully."
            )
            log(f"FAILED: {manifest.error}", err=True)
            return manifest

        for provider_root in sorted(provider_roots, key=lambda item: item.key):
            provider_stats = download_prefix(
                s3_client=s3_client,
                source=provider_root,
                destination=destination_root / "provider",
                allowed_extensions=allowed_extensions,
                dry_run=dry_run,
                max_workers=download_workers,
                verbose=verbose,
            )
            manifest.providers.append(provider_stats)

        all_errors: list[str] = []
        if manifest.payer:
            all_errors.extend(manifest.payer.errors)
        for provider in manifest.providers:
            all_errors.extend(provider.errors)

        if all_errors:
            manifest.status = "completed_with_errors"
            manifest.error = " | ".join(all_errors)
        else:
            manifest.status = "dry_run_completed" if dry_run else "completed"
            manifest.error = None

    except Exception as exc:
        manifest.status = "failed"
        manifest.error = f"{type(exc).__name__}: {exc}"
        if verbose:
            print(f"FATAL ERROR: {manifest.error}", file=sys.stderr)
            traceback.print_exc()

    finally:
        manifest.completed_at_utc = utc_now()
        write_manifest(manifest_path, manifest)
        log(f"manifest: {manifest_path}")
        log(f"status: {manifest.status}")
        log(f"error: {manifest.error}")

    return manifest
