"""DB-backed overrides for the tunable thresholds in config.py.

get_settings() returns a plain dict of every tunable value: config.py's
class attribute is the default, overridden by any row present in the
`settings` table. Callers (classify.py, image_quality.py, etc.) should
always go through get_settings() rather than reading current_app.config
directly for any of these keys, so a change in the Settings page takes
effect immediately without a restart.
"""
import json

from flask import current_app, g

from .db import get_db

# Keys editable from the Settings page, with their type for form parsing.
# Kept as an explicit allowlist so the settings table can never be used to
# override unrelated app config (paths, secrets, etc.) via a crafted POST.
TUNABLE_KEYS = {
    "TEXTRACT_MAX_CONCURRENCY": int,
    "FUZZY_MATCH_THRESHOLD": int,
    "SOUNDEX_TOKEN_OVERLAP_THRESHOLD": float,
    "ENABLE_BLUR_CHECK": bool,
    "BLUR_VARIANCE_THRESHOLD": float,
    "DOCUMENT_WHITE_RATIO_THRESHOLD": float,
    "DOCUMENT_SATURATION_THRESHOLD": float,
    "ENABLE_DUPLICATE_SIGNATURE_CHECK": bool,
    "SIGNATURE_HASH_MATCH_THRESHOLD": int,
    "ENABLE_PASTE_CHECK": bool,
    "ELA_MISMATCH_RATIO_THRESHOLD": float,
    "BORDER_EDGE_DENSITY_THRESHOLD": float,
    "ENABLE_TOO_CLEAN_CHECK": bool,
    "NOISE_FLOOR_TOO_CLEAN_THRESHOLD": float,
    "ENABLE_CROPPED_CHECK": bool,
    "CROP_EDGE_MARGIN_THRESHOLD": float,
    "CROP_EDGE_DENSITY_THRESHOLD": float,
    "ENABLE_CORRECTION_CHECK": bool,
    "CORRECTION_ZSCORE_THRESHOLD": float,
    "CORRECTION_HOTSPOT_MIN_COUNT": int,
    "ENABLE_FACE_CHECK": bool,
    "FACE_CONFIDENCE_THRESHOLD": float,
    "FACE_DETECTION_DPI": int,
}


def get_settings():
    """Every tunable value, DB override merged over config.py defaults.
    Cached on `g` so a single request's many classify() calls don't each
    hit the DB."""
    if "settings" in g:
        return g.settings

    values = {key: current_app.config[key] for key in TUNABLE_KEYS}
    db = get_db()
    for row in db.execute("SELECT key, value FROM settings"):
        if row["key"] in TUNABLE_KEYS:
            values[row["key"]] = json.loads(row["value"])

    g.settings = values
    return values


def update_settings(new_values):
    """new_values: {key: parsed_value} - only keys in TUNABLE_KEYS are
    accepted; anything else is silently ignored (not attacker-controlled
    config)."""
    db = get_db()
    for key, value in new_values.items():
        if key not in TUNABLE_KEYS:
            continue
        db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )
    db.commit()
    g.pop("settings", None)


def reset_settings():
    db = get_db()
    db.execute("DELETE FROM settings")
    db.commit()
    g.pop("settings", None)
