import concurrent.futures
import json
import logging
from datetime import datetime, timezone

from flask import current_app

from . import (cache_store, claim_scanner, classify, face_detector, image_quality,
               page_forensics, page_render, progress, settings_store, signature_forensics,
               textract_client)
from .db import get_db

logger = logging.getLogger(__name__)

# Bumped whenever the set of raw fields stored per page/signature changes,
# so the backfill functions below know to recompute an old cache entry
# (from disk, no Textract call) rather than checking for one specific key.
PAGE_ANALYSIS_VERSION = 3
SIGNATURE_ANALYSIS_VERSION = 2


def _now():
    return datetime.now(timezone.utc).isoformat()


def _start_run_row(claim_id, total_files):
    db = get_db()
    cur = db.execute(
        "INSERT INTO processing_runs (claim_id, started_at, total_files, processed_files, "
        "cached_files, failed_files, status, blurry_pages, photo_pages, document_pages, "
        "unique_documents, suspicious_signatures) "
        "VALUES (?, ?, ?, 0, 0, 0, 'running', 0, 0, 0, 0, 0)",
        (claim_id, _now(), total_files),
    )
    db.commit()
    return cur.lastrowid


def _finish_run_row(run_id, processed, cached, failed, status, blurry_pages, photo_pages,
                     document_pages, unique_documents, suspicious_signatures):
    db = get_db()
    db.execute(
        "UPDATE processing_runs SET finished_at=?, processed_files=?, cached_files=?, "
        "failed_files=?, status=?, blurry_pages=?, photo_pages=?, document_pages=?, "
        "unique_documents=?, suspicious_signatures=? WHERE id=?",
        (_now(), processed, cached, failed, status, blurry_pages, photo_pages, document_pages,
         unique_documents, suspicious_signatures, run_id),
    )
    db.commit()


def get_claim_status(claim_id, latest_run_row=None):
    """Combine the live in-memory progress registry with the last
    processing_runs row for claim_id into one status dict:
    {status: 'not_started'|'running'|'interrupted'|'completed'|'failed',
     total, processed, cached, failed, current_files, total_pages, pages_done,
     blurry_pages, photo_pages, document_pages,
     unique_documents, suspicious_signatures}.
    current_files is a list (not one file) since documents are now
    processed concurrently - several can be in flight at once.
    total_pages/pages_done are Textract-call-level progress, finer-grained
    than the per-document counts since a claim's pages across every
    not-yet-cached document are all queued/analyzed together.
    The quality/forensics rollup counts here are a snapshot from when that
    run finished (computed against whatever settings were active then) -
    routes/claims.py recomputes the *live* versions shown on the claims
    list against current settings; these are only used as a 0/non-zero
    signal while a run is in flight or hasn't started.
    """
    zero_rollups = {"blurry_pages": 0, "photo_pages": 0, "document_pages": 0,
                     "unique_documents": 0, "suspicious_signatures": 0}

    live = progress.get(claim_id)
    if live and live["status"] == "running":
        return {"status": "running",
                **{k: live[k] for k in ("total", "processed", "cached", "failed",
                                         "current_files", "total_pages", "pages_done")},
                **zero_rollups}

    if latest_run_row is None:
        return {"status": "not_started", "total": 0, "processed": 0,
                "cached": 0, "failed": 0, "current_files": [], "total_pages": 0,
                "pages_done": 0, **zero_rollups}

    row_status = latest_run_row["status"]
    if row_status == "running":
        # DB says a run is in progress but nothing is live in this process -
        # most likely the server restarted mid-run.
        row_status = "interrupted"

    return {
        "status": row_status,
        "total": latest_run_row["total_files"] or 0,
        "processed": latest_run_row["processed_files"] or 0,
        "cached": latest_run_row["cached_files"] or 0,
        "failed": latest_run_row["failed_files"] or 0,
        "current_files": [],
        "total_pages": 0,
        "pages_done": 0,
        "blurry_pages": latest_run_row["blurry_pages"] or 0,
        "photo_pages": latest_run_row["photo_pages"] or 0,
        "document_pages": latest_run_row["document_pages"] or 0,
        "unique_documents": latest_run_row["unique_documents"] or 0,
        "suspicious_signatures": latest_run_row["suspicious_signatures"] or 0,
    }


def _analyze_page_raw(jpeg_bytes, signature_bboxes):
    """Every raw, unthresholded per-page measurement in one place: blur/
    content-type/noise (image_quality), cropped edges + correction
    candidates (page_forensics), face detections (face_detector - local/
    CPU-only, no AWS cost, same as the others here). Tagged with the
    current schema version so backfill can detect stale cache entries."""
    raw = {}
    raw.update(image_quality.analyze_page_quality(jpeg_bytes))
    raw.update(page_forensics.detect_cropped_edges(jpeg_bytes))
    raw.update(page_forensics.detect_correction_hotspots(jpeg_bytes, exclude_bboxes=signature_bboxes))
    raw["faces"] = face_detector.detect_faces(jpeg_bytes)
    raw["_v"] = PAGE_ANALYSIS_VERSION
    return raw


def _backfill_page_analysis(cached):
    """Mutates `cached` in place, recomputing raw page measurements for any
    page whose schema version is stale, from its already-rendered image on
    disk - no Textract/AWS call. Returns True if anything changed."""
    changed = False
    for page in cached["pages"]:
        if page.get("_v") == PAGE_ANALYSIS_VERSION:
            continue
        image_path = cache_store.page_image_path(page["image_rel"])
        with open(image_path, "rb") as f:
            jpeg_bytes = f.read()
        bboxes = [s["bbox"] for s in page["signatures"]]
        page.update(_analyze_page_raw(jpeg_bytes, bboxes))
        changed = True
    return changed


def _annotate_signatures(jpeg_bytes, signatures):
    """Adds phash + raw paste-detection measurements to any signature whose
    schema version is stale. Safe to call unconditionally."""
    for sig in signatures:
        if sig.get("_v") == SIGNATURE_ANALYSIS_VERSION:
            continue
        crop = signature_forensics.crop_signature(jpeg_bytes, sig["bbox"])
        sig["phash"] = signature_forensics.compute_hash(crop)
        sig.update(signature_forensics.detect_pasted_patch(jpeg_bytes, sig["bbox"]))
        sig["_v"] = SIGNATURE_ANALYSIS_VERSION
    return signatures


def _backfill_signature_forensics(cached):
    """Mutates `cached` in place, computing phash/paste measurements for any
    signature whose schema version is stale, from the already-rendered page
    image on disk - no Textract/AWS call. Returns True if anything changed."""
    changed = False
    for page in cached["pages"]:
        if not page["signatures"] or all(s.get("_v") == SIGNATURE_ANALYSIS_VERSION for s in page["signatures"]):
            continue
        image_path = cache_store.page_image_path(page["image_rel"])
        with open(image_path, "rb") as f:
            jpeg_bytes = f.read()
        _annotate_signatures(jpeg_bytes, page["signatures"])
        changed = True
    return changed


def _reindex_signatures(claim_id, doc_rel_path, cached):
    """Re-derives cross-document duplicate-signature *candidates* for this
    document against every other document's signatures processed so far
    (any claim under the root, per the whole-root fraud-signal design), and
    refreshes this document's own rows in signature_index. Stores the
    nearest SIGNATURE_HASH_STORE_LIMIT candidates by distance regardless of
    today's match threshold (as `duplicate_candidates`), so classify.py can
    filter to the *current* threshold live without reprocessing. Mutates
    each signature dict in `cached`. Always returns True (candidates are
    recomputed - and may change - on every run)."""
    db = get_db()
    file_hash = cached["file_hash"]
    store_limit = current_app.config["SIGNATURE_HASH_STORE_LIMIT"]

    db.execute("DELETE FROM signature_index WHERE file_hash=?", (file_hash,))
    candidates = db.execute(
        "SELECT claim_id, document_path, page_number, phash FROM signature_index "
        "WHERE file_hash != ? AND phash != ''",
        (file_hash,),
    ).fetchall()

    any_signatures = False
    for page in cached["pages"]:
        for sig in page["signatures"]:
            any_signatures = True
            matches = []
            if sig.get("phash"):
                for cand in candidates:
                    distance = int(signature_forensics.hash_distance(sig["phash"], cand["phash"]))
                    matches.append({
                        "claim_id": cand["claim_id"],
                        "document_path": cand["document_path"],
                        "page_number": cand["page_number"],
                        "distance": distance,
                    })
                matches.sort(key=lambda m: m["distance"])
                matches = matches[:store_limit]
            sig["duplicate_candidates"] = matches

            db.execute(
                "INSERT INTO signature_index (claim_id, document_path, file_hash, page_number, "
                "signature_id, phash, bbox_json, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (claim_id, doc_rel_path, file_hash, page["page_number"], sig["signature_id"],
                 sig.get("phash") or "", json.dumps(sig["bbox"]), sig.get("confidence")),
            )
    db.commit()
    return any_signatures


def _build_page_result(file_hash, page, analysis):
    signatures = _annotate_signatures(page["jpeg_bytes"], analysis["signatures"])
    page_raw = _analyze_page_raw(page["jpeg_bytes"], [s["bbox"] for s in signatures])
    return {
        "page_number": page["page_number"],
        "image_rel": f"{file_hash}/page-{page['page_number']}.jpg",
        "width": page["width"],
        "height": page["height"],
        "forms": analysis["forms"],
        "tables": analysis["tables"],
        "signatures": signatures,
        "queries": analysis["queries"],
        **page_raw,
    }


def process_document(doc, settings, region=None, queries=None):
    """Process a single scanned document dict (from claim_scanner.scan_claim)
    if not already cached. Returns (outcome, data) where outcome is 'cached'
    or 'processed' and data is the full cached-result dict (backfilled with
    any stale raw quality/signature-forensics measurements). `settings` is
    only used for FACE_DETECTION_DPI (which PDF pages are rasterized at) -
    everything else here is stored raw and classified live.

    Kept as a simple sequential single-document entry point (e.g. for
    tests); process_claim below does its own concurrent version of this
    same logic across every document in a claim at once - see its
    docstring. Needs a Flask app context (reads current_app.config) unless
    `region`/`queries` are passed in explicitly.
    """
    if region is None or queries is None:
        region = current_app.config["AWS_REGION"]
        queries = current_app.config["DEFAULT_QUERIES"]

    file_hash = cache_store.hash_file(doc["abs_path"])
    cached = cache_store.load_cached_result(file_hash)
    if cached is not None:
        changed = _backfill_signature_forensics(cached)
        changed = _backfill_page_analysis(cached) or changed
        if changed:
            cache_store.save_cached_result(file_hash, cached)
        return "cached", cached

    pages_dir = cache_store.pages_cache_dir(file_hash)
    rendered = page_render.save_page_images(doc["abs_path"], pages_dir, dpi=settings.get("FACE_DETECTION_DPI"))

    page_results = []
    for page in rendered:
        analysis = textract_client.analyze_page(page["jpeg_bytes"], region, queries)
        page_results.append(_build_page_result(file_hash, page, analysis))

    result = {
        "file_name": doc["rel_path"],
        "file_hash": file_hash,
        "num_pages": len(page_results),
        "pages": page_results,
    }
    cache_store.save_cached_result(file_hash, result)
    return "processed", result


def process_claim(app, claim_id, claim_path):
    """Runs in a background thread - pushes its own app context so
    current_app/g (config, db) work outside the request lifecycle.

    Every page across every not-yet-cached document in the claim gets its
    Textract call queued up front and run concurrently on a shared thread
    pool (bounded by the TEXTRACT_MAX_CONCURRENCY setting, enforced
    *globally* via textract_client's process-wide semaphore - so batch-
    processing several claims at once still respects one overall cap, not
    one per claim) instead of one call at a time, one document at a time.
    Progress updates page-by-page, in whatever order calls actually finish
    in - not batched by submission order - so the UI reflects real
    concurrent progress rather than a simulated sequential one.
    """
    with app.app_context():
        settings = settings_store.get_settings()
        region = current_app.config["AWS_REGION"]
        queries = current_app.config["DEFAULT_QUERIES"]
        docs = claim_scanner.scan_claim(claim_path)
        progress.start(claim_id, len(docs))
        run_id = _start_run_row(claim_id, len(docs))
        max_concurrency = max(1, int(settings.get("TEXTRACT_MAX_CONCURRENCY", 4)))

        processed = cached = failed = 0
        blurry_pages = photo_pages = document_pages = suspicious_signatures = 0
        unique_hashes = set()

        def _finalize(doc, data, outcome):
            nonlocal processed, cached, blurry_pages, photo_pages, document_pages, suspicious_signatures
            unique_hashes.add(data["file_hash"])
            try:
                _reindex_signatures(claim_id, doc["rel_path"], data)
                cache_store.save_cached_result(data["file_hash"], data)
            except Exception:
                # The document itself processed fine - signature cross-
                # referencing is best-effort on top of that, so a failure
                # here shouldn't flip an already-counted doc to "failed".
                logger.exception("Signature reindexing failed for %s", doc["abs_path"])

            for page in data["pages"]:
                page_tags = classify.classify_page(page, settings)
                if page_tags["is_blurry"]:
                    blurry_pages += 1
                if page_tags["content_type"] == "photo":
                    photo_pages += 1
                elif page_tags["content_type"] == "document":
                    document_pages += 1
                for sig in page["signatures"]:
                    sig_tags = classify.classify_signature(sig, settings)
                    if sig_tags["paste_suspicious"] or sig_tags["possible_duplicate_of"]:
                        suspicious_signatures += 1

            if outcome == "cached":
                cached += 1
                progress.increment(claim_id, "cached")
            else:
                processed += 1
                progress.increment(claim_id, "processed")

        with concurrent.futures.ThreadPoolExecutor(max_workers=max(4, max_concurrency * 2)) as executor:
            pending = []
            future_map = {}

            # Phase 1: cache hits are cheap (no Textract) - finalize them
            # immediately, sequentially. Cache misses get rendered (local,
            # fast) and every one of their pages' Textract calls submitted
            # right away, so a claim with many documents analyzes all of
            # them concurrently instead of strictly one at a time.
            for doc in docs:
                file_hash = cache_store.hash_file(doc["abs_path"])
                cached_result = cache_store.load_cached_result(file_hash)
                if cached_result is not None:
                    changed = _backfill_signature_forensics(cached_result)
                    changed = _backfill_page_analysis(cached_result) or changed
                    if changed:
                        cache_store.save_cached_result(file_hash, cached_result)
                    _finalize(doc, cached_result, "cached")
                    continue

                try:
                    pages_dir = cache_store.pages_cache_dir(file_hash)
                    rendered = page_render.save_page_images(
                        doc["abs_path"], pages_dir, dpi=settings.get("FACE_DETECTION_DPI"))
                except Exception:
                    failed += 1
                    progress.increment(claim_id, "failed")
                    logger.exception("Failed to render %s", doc["abs_path"])
                    continue

                progress.add_in_flight(claim_id, doc["rel_path"])
                progress.increment(claim_id, "total_pages", len(rendered))
                item = {"doc": doc, "file_hash": file_hash, "rendered": rendered,
                        "remaining": len(rendered), "page_results": {}, "failed": False}
                pending.append(item)
                for page in rendered:
                    future = executor.submit(
                        textract_client.analyze_page_throttled,
                        page["jpeg_bytes"], region, queries, max_concurrency,
                    )
                    future_map[future] = (item, page)

            # Phase 2: gather each page's result as soon as it's ready -
            # real-time progress instead of batched by submission order.
            for future in concurrent.futures.as_completed(future_map):
                item, page = future_map[future]
                doc = item["doc"]
                try:
                    analysis = future.result()
                    item["page_results"][page["page_number"]] = _build_page_result(item["file_hash"], page, analysis)
                except Exception:
                    item["failed"] = True
                    logger.exception("Textract call failed for %s page %s", doc["abs_path"], page["page_number"])
                progress.increment(claim_id, "pages_done")
                item["remaining"] -= 1

                if item["remaining"] > 0:
                    continue

                progress.remove_in_flight(claim_id, doc["rel_path"])
                if item["failed"]:
                    failed += 1
                    progress.increment(claim_id, "failed")
                    continue

                ordered_pages = [item["page_results"][p["page_number"]] for p in item["rendered"]]
                data = {"file_name": doc["rel_path"], "file_hash": item["file_hash"],
                        "num_pages": len(ordered_pages), "pages": ordered_pages}
                cache_store.save_cached_result(item["file_hash"], data)
                _finalize(doc, data, "processed")

        final_status = "completed" if failed == 0 else "failed"
        progress.finish(claim_id, status=final_status)
        _finish_run_row(run_id, processed, cached, failed, final_status,
                         blurry_pages, photo_pages, document_pages,
                         len(unique_hashes), suspicious_signatures)
