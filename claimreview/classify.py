"""Turns raw measurements (from image_quality.py, signature_forensics.py,
page_forensics.py) into the tags shown in the UI, against the *current*
settings (settings_store.get_settings()). Called fresh every time cached
data is displayed - never trust a boolean baked into the cache, since the
whole point is that retuning a threshold re-labels every already-processed
page/signature instantly.

Every function here is a pure function of (raw_metrics, settings) - no I/O,
no Textract, no reprocessing (the one exception being the 'gps photo'
keyword check below, which re-scans a page's already-cached Textract text -
still no AWS call, no disk I/O beyond what's already in memory).
"""

from . import search

# Keywords a GPS-camera app typically stamps onto a photo (location, map
# attribution, device info) - a hedged heuristic, not proof the photo is
# geotagged, just enough to separate "site photo with an overlay" from a
# plain photo for rule-scoping purposes.
GPS_PHOTO_KEYWORDS = ["Lat", "Lon", "GPS", "Google", "Camera"]
_GPS_PHOTO_PATTERN = r"\b(?:" + "|".join(GPS_PHOTO_KEYWORDS) + r")\b"


def _has_gps_keywords(page):
    kvs = search.page_kv_dict(page)
    if not kvs:
        return False
    result = search.search_keyword({"page": kvs}, _GPS_PHOTO_PATTERN, regex=True, case_insensitive=True)
    return result["best"] is not None


def classify_page_quality(raw, settings):
    """{is_blurry, content_type} from blur_score/white_ratio/mean_saturation.
    content_type is 'document', 'photo', or 'gps photo' - the last one a
    'photo' page whose extracted text mentions GPS_PHOTO_KEYWORDS (a GPS
    camera app's location/map overlay)."""
    if raw.get("blur_score") is None:
        return {"is_blurry": False, "content_type": None}

    is_blurry = settings["ENABLE_BLUR_CHECK"] and raw["blur_score"] < settings["BLUR_VARIANCE_THRESHOLD"]
    white_ratio = raw.get("white_ratio")
    mean_saturation = raw.get("mean_saturation")
    is_document = (white_ratio is not None and mean_saturation is not None
                   and white_ratio >= settings["DOCUMENT_WHITE_RATIO_THRESHOLD"]
                   and mean_saturation <= settings["DOCUMENT_SATURATION_THRESHOLD"])
    if is_document:
        content_type = "document"
    elif _has_gps_keywords(raw):
        content_type = "gps photo"
    else:
        content_type = "photo"
    return {"is_blurry": is_blurry, "content_type": content_type}


def classify_too_clean(raw, settings):
    """{too_clean_suspected} from noise_sigma - a native/digitally-generated
    page has almost no measurable scan/sensor noise."""
    if not settings["ENABLE_TOO_CLEAN_CHECK"] or raw.get("noise_sigma") is None:
        return {"too_clean_suspected": False}
    return {"too_clean_suspected": raw["noise_sigma"] <= settings["NOISE_FLOOR_TOO_CLEAN_THRESHOLD"]}


def classify_cropped(raw, settings):
    """{cropped_suspected, edges} from per-edge ink margin/density."""
    if not settings["ENABLE_CROPPED_CHECK"] or not raw.get("edges"):
        return {"cropped_suspected": False, "edges": []}

    flagged = [
        name for name, edge in raw["edges"].items()
        if edge["margin_px"] <= settings["CROP_EDGE_MARGIN_THRESHOLD"]
        and edge["density"] >= settings["CROP_EDGE_DENSITY_THRESHOLD"]
    ]
    return {"cropped_suspected": bool(flagged), "edges": flagged}


def classify_correction(raw, settings):
    """{correction_suspected, hotspot_count} from a page's candidate
    ink-density anomaly clusters (stored at a permissive exploratory
    z-score so the real threshold can be tuned live). Off by default and
    hedged as low-confidence in the UI - stamps, tables, and dense
    signatures can also trigger it."""
    if not settings["ENABLE_CORRECTION_CHECK"]:
        return {"correction_suspected": False, "hotspot_count": 0}
    threshold = settings["CORRECTION_ZSCORE_THRESHOLD"]
    count = sum(1 for c in (raw.get("correction_candidates") or []) if c["zscore"] >= threshold)
    return {"correction_suspected": count >= settings["CORRECTION_HOTSPOT_MIN_COUNT"],
            "hotspot_count": count}


def classify_paste(raw, settings):
    """{paste_suspicious, notes} from ela_ratio/border_edge_density."""
    if not settings["ENABLE_PASTE_CHECK"]:
        return {"paste_suspicious": False, "notes": []}
    notes = []
    if raw.get("ela_ratio") is not None and raw["ela_ratio"] >= settings["ELA_MISMATCH_RATIO_THRESHOLD"]:
        notes.append("recompression error level differs from surrounding page")
    if raw.get("border_edge_density") is not None and raw["border_edge_density"] >= settings["BORDER_EDGE_DENSITY_THRESHOLD"]:
        notes.append("sharp rectangular border detected around signature")
    return {"paste_suspicious": bool(notes), "notes": notes}


def classify_duplicates(candidates, settings):
    """Filters a signature's stored nearest-neighbor candidate list
    (each {claim_id, document_path, page_number, distance}) down to the
    ones clearing the *current* match threshold, closest first."""
    if not settings["ENABLE_DUPLICATE_SIGNATURE_CHECK"]:
        return []
    threshold = settings["SIGNATURE_HASH_MATCH_THRESHOLD"]
    matches = [c for c in (candidates or []) if c["distance"] <= threshold]
    matches.sort(key=lambda c: c["distance"])
    return matches


def classify_faces(raw, settings):
    """{has_face, faces} from a page's stored raw face detections (captured
    liberally at face_detector's fixed internal confidence floor) - filters
    down to the ones clearing the *current* FACE_CONFIDENCE_THRESHOLD."""
    if not settings["ENABLE_FACE_CHECK"]:
        return {"has_face": False, "faces": []}
    threshold = settings["FACE_CONFIDENCE_THRESHOLD"]
    matches = [f for f in (raw.get("faces") or []) if f["confidence"] >= threshold]
    return {"has_face": bool(matches), "faces": matches}


def classify_page(page, settings):
    """Full derived-tag set for one cached page dict (as stored under
    cached_result["pages"][i]): quality + too-clean + cropped + correction
    + faces."""
    result = {}
    result.update(classify_page_quality(page, settings))
    result.update(classify_too_clean(page, settings))
    result.update(classify_cropped(page, settings))
    result.update(classify_correction(page, settings))
    result.update(classify_faces(page, settings))
    return result


def classify_signature(sig, settings):
    """Full derived-tag set for one cached signature dict: paste +
    duplicate matches (filtered from the stored candidate list)."""
    result = classify_paste(sig, settings)
    result["possible_duplicate_of"] = classify_duplicates(sig.get("duplicate_candidates"), settings)
    return result
