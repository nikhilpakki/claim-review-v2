from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_from_directory, url_for

from .. import cache_store, claim_scanner, classify, root_state, settings_store

bp = Blueprint("documents", __name__)


def _load_doc_cache(claim_path, doc_id):
    abs_path = claim_scanner.resolve_doc(claim_path, doc_id)
    file_hash = cache_store.hash_file(abs_path)
    cached = cache_store.load_cached_result(file_hash)
    return cached, file_hash


def _classified_page(page_data, settings):
    """page_data with derived tags merged in live (page-level: is_blurry,
    content_type, too_clean_suspected, cropped_suspected,
    correction_suspected; per-signature: paste_suspicious, notes,
    possible_duplicate_of) - never trusts whatever was last baked into the
    cache, so a Settings change is reflected immediately."""
    page_tags = classify.classify_page(page_data, settings)
    signatures = []
    for sig in page_data.get("signatures", []):
        sig_tags = classify.classify_signature(sig, settings)
        signatures.append({**sig, **sig_tags})
    return {**page_data, **page_tags, "signatures": signatures}


@bp.route("/claims/<claim_id>/doc/<path:doc_id>/page/<int:page>")
def view_page(claim_id, doc_id, page):
    try:
        claim_path = root_state.get_claim_path(claim_id)
        cached, _file_hash = _load_doc_cache(claim_path, doc_id)
    except (ValueError, FileNotFoundError):
        abort(404)

    if not cached:
        return render_template("document_view.html", claim_id=claim_id, doc_id=doc_id,
                                page=None, page_data=None, num_pages=0,
                                highlight=request.args.to_dict())

    page_data = next((p for p in cached["pages"] if p["page_number"] == page), None)
    if page_data is None:
        abort(404)

    settings = settings_store.get_settings()
    return render_template("document_view.html", claim_id=claim_id, doc_id=doc_id,
                            page=page, page_data=_classified_page(page_data, settings),
                            num_pages=cached["num_pages"], highlight=request.args.to_dict())


@bp.route("/media/pages/<file_hash>/<filename>")
def media_page(file_hash, filename):
    directory = cache_store.pages_cache_dir(file_hash)
    return send_from_directory(directory, filename)


@bp.route("/api/claims/<claim_id>/doc/<path:doc_id>/page/<int:page>/blocks")
def api_page_blocks(claim_id, doc_id, page):
    try:
        claim_path = root_state.get_claim_path(claim_id)
        cached, _file_hash = _load_doc_cache(claim_path, doc_id)
    except (ValueError, FileNotFoundError):
        return jsonify({"error": "Not found"}), 404

    if not cached:
        return jsonify({"error": "Not processed yet"}), 404

    page_data = next((p for p in cached["pages"] if p["page_number"] == page), None)
    if page_data is None:
        return jsonify({"error": "Page not found"}), 404

    settings = settings_store.get_settings()
    response = _classified_page(page_data, settings)
    file_hash, filename = page_data["image_rel"].split("/")
    response["image_url"] = url_for("documents.media_page", file_hash=file_hash, filename=filename)
    response["num_pages"] = cached["num_pages"]
    response["file_name"] = cached["file_name"]
    response["doc_id"] = doc_id
    response["claim_id"] = claim_id
    response["standalone_url"] = url_for("documents.view_page", claim_id=claim_id, doc_id=doc_id, page=page)
    return jsonify(response)


@bp.route("/api/claims/<claim_id>/signatures")
def api_signatures(claim_id):
    try:
        claim_path = root_state.get_claim_path(claim_id)
    except (ValueError, FileNotFoundError):
        return jsonify({"error": "Claim not found"}), 404

    settings = settings_store.get_settings()
    signatures = []
    seen_hashes = set()
    for doc in claim_scanner.scan_claim(claim_path):
        file_hash = cache_store.hash_file(doc["abs_path"])
        if file_hash in seen_hashes:
            # identical content already represented by an earlier duplicate
            # of this same file - its signatures would just repeat verbatim.
            continue
        seen_hashes.add(file_hash)

        cached = cache_store.load_cached_result(file_hash)
        if not cached:
            continue
        for page in cached["pages"]:
            for sig in page["signatures"]:
                sig_tags = classify.classify_signature(sig, settings)
                signatures.append({
                    "file": doc["rel_path"],
                    "page_number": page["page_number"],
                    "signature_id": sig["signature_id"],
                    "confidence": sig["confidence"],
                    "bbox": sig["bbox"],
                    "paste_suspicious": sig_tags["paste_suspicious"],
                    "paste_notes": sig_tags["notes"],
                    "possible_duplicate_of": sig_tags["possible_duplicate_of"],
                })
    return jsonify({"signatures": signatures})
