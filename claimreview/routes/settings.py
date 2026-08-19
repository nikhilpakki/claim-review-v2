from urllib.parse import quote

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from .. import settings_store

bp = Blueprint("settings", __name__)


def _safe_next():
    """The `next` query param, if it's a same-site path (never an absolute
    URL / protocol-relative `//host` - that would be an open redirect)."""
    value = request.args.get("next")
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return None


def _next_qs():
    """?next=... to append to this page's own form actions, so the
    return-to destination survives a POST-redirect-GET round trip."""
    value = _safe_next()
    return f"?next={quote(value, safe='')}" if value else ""


def _is_ajax():
    """True for the fetch-based submissions settings.html's JS makes (in
    place of a full-page form POST) - lets one route serve both a plain
    HTML form (redirect, for JS-disabled/no-JS fallback) and the AJAX
    "save without leaving the page" flow (JSON) from the same endpoint."""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"

# (key, label, help text) grouped for display. Purely presentational -
# settings_store.TUNABLE_KEYS is the source of truth for what's editable.
FIELD_GROUPS = [
    ("Processing", [
        ("TEXTRACT_MAX_CONCURRENCY", "Max concurrent Textract calls", "How many analyze_document calls run at once, across every claim being processed at the same time (not per claim) - keep this under your AWS account's Textract TPS quota. Raising it speeds up processing; too high causes throttling retries instead."),
    ]),
    ("Search", [
        ("FUZZY_MATCH_THRESHOLD", "Fuzzy match threshold", "0-100 rapidfuzz score; lower catches more typos/OCR noise but more false matches."),
        ("SOUNDEX_TOKEN_OVERLAP_THRESHOLD", "Soundex overlap threshold", "0-1 fraction of phonetic tokens that must match for the last-resort search fallback."),
    ]),
    ("Blur / content type", [
        ("ENABLE_BLUR_CHECK", "Enable blurry-page check", "Off hides the blurry-page tag/count everywhere (document list, claims list, rules) without discarding the underlying sharpness measurement - re-enabling shows it again instantly, no reprocessing. Document/photo/gps-photo classification below is unaffected."),
        ("BLUR_VARIANCE_THRESHOLD", "Blur variance threshold", "Below this sharpness score, a page is flagged blurry. Raise it if sharp pages are being flagged; lower it if blurry pages are being missed."),
        ("DOCUMENT_WHITE_RATIO_THRESHOLD", "Document white-ratio threshold", "Fraction of near-white pixels above which a page looks like a scanned document rather than a photo."),
        ("DOCUMENT_SATURATION_THRESHOLD", "Document saturation threshold", "Color saturation below which a page looks like a scanned document rather than a photo."),
    ]),
    ("Too clean / possibly not a scan", [
        ("ENABLE_TOO_CLEAN_CHECK", "Enable this check", None),
        ("NOISE_FLOOR_TOO_CLEAN_THRESHOLD", "Noise floor threshold", "Below this estimated sensor/scan noise level, a page looks suspiciously clean (possibly a native digital PDF, not a physical scan)."),
    ]),
    ("Cropped / cut-off page", [
        ("ENABLE_CROPPED_CHECK", "Enable this check", None),
        ("CROP_EDGE_MARGIN_THRESHOLD", "Edge margin threshold (px)", "Content within this many pixels of the image border counts as \"touching\" it."),
        ("CROP_EDGE_DENSITY_THRESHOLD", "Edge density threshold", "0-1 fraction of ink pixels along the border strip required to flag a cropped edge (avoids flagging a single stray mark)."),
    ]),
    ("Possible correction / strikethrough (lower confidence)", [
        ("ENABLE_CORRECTION_CHECK", "Enable this check", "Off by default - this is the least reliable of the checks and can flag stamps, tables, or dense signatures."),
        ("CORRECTION_ZSCORE_THRESHOLD", "Anomaly z-score threshold", "How much denser than the page's own average ink density a small region must be to get flagged."),
        ("CORRECTION_HOTSPOT_MIN_COUNT", "Minimum hotspot count", "Number of anomalous regions required before a page is flagged."),
    ]),
    ("Duplicate signatures", [
        ("ENABLE_DUPLICATE_SIGNATURE_CHECK", "Enable this check", "Off hides the possible-duplicate-signature tag/count everywhere without discarding the stored candidate matches - re-enabling shows them again instantly, no reprocessing."),
        ("SIGNATURE_HASH_MATCH_THRESHOLD", "Duplicate-signature match threshold", "Max hash distance (0-256) for two signatures to count as matching. Lower is stricter (fewer, more confident matches)."),
    ]),
    ("Possible signature paste", [
        ("ENABLE_PASTE_CHECK", "Enable this check", "Off hides the possible-paste tag everywhere without discarding the underlying measurements - re-enabling shows it again instantly, no reprocessing."),
        ("ELA_MISMATCH_RATIO_THRESHOLD", "Recompression ratio threshold", "How different a signature's compression-error level must be from the page around it to flag a possible paste."),
        ("BORDER_EDGE_DENSITY_THRESHOLD", "Border edge density threshold", "0-1 fraction of the ring around a signature showing a straight seam-like edge to flag a possible paste."),
    ]),
    ("Face detection ('has face' tag)", [
        ("ENABLE_FACE_CHECK", "Enable this check", "Off hides the 'has face' tag/count everywhere without discarding the underlying detections - re-enabling shows it again instantly, no reprocessing."),
        ("FACE_CONFIDENCE_THRESHOLD", "Face confidence threshold", "0-1 detector score a face must clear to count. Lower catches more faces but more false positives; the Haar-cascade fallback (used only if the YuNet model file is missing) has no real confidence score and always reports 1.0."),
        ("FACE_DETECTION_DPI", "PDF rasterization DPI", "Resolution used to render PDF pages when a document is first processed - higher can help detect small/distant faces. Only affects documents processed after this is changed; already-processed documents need a real reprocess to pick up a new value."),
    ]),
]


@bp.route("/settings")
def view_settings():
    values = settings_store.get_settings()
    return render_template("settings.html", groups=FIELD_GROUPS, values=values,
                            types=settings_store.TUNABLE_KEYS,
                            back_url=_safe_next() or url_for("claims.list_claims_view"),
                            next_qs=_next_qs())


@bp.route("/settings", methods=["POST"])
def save_settings():
    parsed = {}
    for key, py_type in settings_store.TUNABLE_KEYS.items():
        if py_type is bool:
            parsed[key] = request.form.get(key) == "on"
            continue
        raw = request.form.get(key)
        if raw is None or raw.strip() == "":
            continue
        try:
            parsed[key] = py_type(raw)
        except ValueError:
            continue
    settings_store.update_settings(parsed)
    if _is_ajax():
        return jsonify({"status": "saved", "values": settings_store.get_settings()})
    return redirect(url_for("settings.view_settings", next=_safe_next()))


@bp.route("/settings/reset", methods=["POST"])
def reset_settings():
    settings_store.reset_settings()
    if _is_ajax():
        return jsonify({"status": "reset", "values": settings_store.get_settings()})
    return redirect(url_for("settings.view_settings", next=_safe_next()))
