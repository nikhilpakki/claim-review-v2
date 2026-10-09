import os
from datetime import datetime

from flask import (Blueprint, current_app, jsonify, redirect, render_template,
                   request, stream_with_context, url_for)

from .. import (claim_amounts, claim_export, claim_extract, claim_scanner, claim_sections,
                claim_summary, classify, csv_data, processing, rollup_cache, root_state,
                rules_engine, settings_store, textract_profile, users)
from ..db import get_latest_runs
from ..fetch import runs as fetch_runs

bp = Blueprint("claims", __name__)


def _empty_rollup():
    return {"blurry_pages": 0, "too_clean_pages": 0, "cropped_pages": 0,
            "correction_pages": 0, "paste_signatures": 0, "duplicate_signatures": 0,
            "face_pages": 0, "rule_violations": 0}


def _claim_rollup(claim_id, claim_path, docs, settings, user_id=None, rules=None):
    """Live counts for the claims-list badges, computed fresh against
    *current* settings/rules from each already-cached, deduped document -
    not a processing_runs snapshot - so retuning a threshold or rule
    updates these immediately, with no reprocessing or AWS cost. `docs` is
    an already-scanned claim_scanner.scan_claim_cached() list, shared with
    the rules evaluation below so the claim's files are only hashed/read
    once per page load, not once per rollup metric plus once per rule."""
    rollup = _empty_rollup()
    seen_hashes = set()
    for doc in docs:
        file_hash = doc["file_hash"]
        if file_hash in seen_hashes:
            continue
        seen_hashes.add(file_hash)
        cached_result = doc["cached_result"]
        if not cached_result:
            continue
        for page in cached_result.get("pages", []):
            tags = classify.classify_page(page, settings)
            if tags["is_blurry"]:
                rollup["blurry_pages"] += 1
            if tags["too_clean_suspected"]:
                rollup["too_clean_pages"] += 1
            if tags["cropped_suspected"]:
                rollup["cropped_pages"] += 1
            if tags["correction_suspected"]:
                rollup["correction_pages"] += 1
            if tags["has_face"]:
                rollup["face_pages"] += 1
            for sig in page.get("signatures", []):
                sig_tags = classify.classify_signature(sig, settings)
                if sig_tags["paste_suspicious"]:
                    rollup["paste_signatures"] += 1
                if sig_tags["possible_duplicate_of"]:
                    rollup["duplicate_signatures"] += 1

    rollup["rule_violations"] = sum(
        1 for r in rules_engine.evaluate_rules(claim_id, claim_path, docs=docs,
                                               settings=settings, user_id=user_id,
                                               rules=rules)
        if r["status"] == "fail")
    return rollup


def _fetch_run_context(claim_ids):
    """?fetch_run=<id> - the claims a fetch just landed, pre-selected here so
    the reviewer can process exactly that batch without hand-picking it out of
    a folder that may hold hundreds of older claims.

    Only claims that actually produced a bundle are pre-selected; a claim whose
    download failed has no folder to review. Returns (preselect_ids, banner) -
    the banner also covers the mismatch case, where the run's claims are not in
    the folder currently being reviewed."""
    run_id = request.args.get("fetch_run")
    if not run_id:
        return set(), None

    run = fetch_runs.get_run(run_id)
    if run is None:
        return set(), {"run_id": run_id, "missing": True}

    landed = set(fetch_runs.run_claim_ids(run_id, successful_only=True))
    present = landed & set(claim_ids)
    return present, {
        "run_id": run_id,
        "missing": False,
        "landed": len(landed),
        "present": len(present),
        "destination": run["destination"],
        "status": run["status"],
    }


@bp.route("/claims")
def list_claims_view():
    root = root_state.get_active_root()
    if not root:
        return redirect(url_for("browse.browse"))
    claims = claim_scanner.list_claims(root)
    latest_runs = get_latest_runs()
    settings = settings_store.get_settings()
    preselect, fetch_run = _fetch_run_context([c["claim_id"] for c in claims])
    # Which fetch run each claim came from, so a folder holding several batches
    # is legible rather than one undifferentiated list.
    claim_runs = fetch_runs.claim_run_map()
    fetch_run_options = fetch_runs.runs_with_claims(limit=25)

    # Rollups are memoized per claim (see rollup_cache.py). The key covers the
    # claim's documents, the settings, the rules and the claims dataset, so a
    # hit is only taken when recomputing would produce the same answer - live
    # retuning still shows up immediately, it just does not re-read and
    # re-classify every claim in the folder on every page load.
    user_id = users.current_user_id()
    global_fp = rollup_cache.global_fingerprint(settings, user_id)
    cached_rollups = rollup_cache.get_all(user_id)
    extract_versions = claim_extract.versions()
    # The rule set is the same for every claim in the folder, so it is read once
    # here instead of once per claim (two queries each, thousands of times over).
    rules = rules_engine.list_rules(user_id)
    fresh = []

    for claim in claims:
        claim_id = claim["claim_id"]
        status = processing.get_claim_status(claim_id, latest_runs.get(claim_id))
        if status["status"] in ("completed", "failed"):
            docs = claim_scanner.scan_claim_hashed(claim["path"])
            key = rollup_cache.claim_key(global_fp, (d["file_hash"] for d in docs),
                                         extract_versions.get(claim_id))
            hit = cached_rollups.get(claim_id)
            if hit and hit[0] == key:
                status["rollup"] = hit[1]
            else:
                claim_scanner.attach_cached_results(docs)
                rollup = _claim_rollup(claim_id, claim["path"], docs, settings, user_id,
                                       rules=rules)
                status["rollup"] = rollup
                fresh.append((claim_id, key, rollup))
        else:
            status["rollup"] = _empty_rollup()
        claim["status"] = status
        claim["preselected"] = claim_id in preselect
        claim["fetch_run"] = claim_runs.get(claim_id)

    rollup_cache.put_many(fresh, user_id)
    # Only offer runs whose claims are actually in this folder - a run that
    # downloaded somewhere else would filter the list down to nothing.
    present = {c["claim_id"] for c in claims}
    here_counts = {}
    for claim_id in present:
        for run_id in (claim_runs.get(claim_id, {}).get("run_ids") or []):
            here_counts[run_id] = here_counts.get(run_id, 0) + 1
    # The count shown is claims from that run *in this folder*, which is what
    # the filter will actually reveal - a run's own total can include claims
    # whose download failed, or that were downloaded somewhere else.
    fetch_run_options = [
        dict(run, claims_here=here_counts[run["run_id"]])
        for run in fetch_run_options if run["run_id"] in here_counts
    ]
    return render_template("claims_list.html", claims=claims, active_root=root,
                           settings=settings, fetch_run=fetch_run,
                           fetch_run_options=fetch_run_options,
                           selected_run_id=request.args.get("fetch_run") or "")


@bp.route("/claims/export.csv")
def export_claims_csv():
    """The fully processed claims in the active folder, as one CSV row each.

    `?fetch_run=<id>` narrows it to that batch; without it, the whole folder. A
    folder accumulates every fetch ever run into it, so exporting all of it is
    rarely what somebody reviewing today's batch wants.

    Streamed. A folder of a few thousand claims means re-reading every cached
    document, which takes long enough that a buffered response would look like
    a hung tab; streaming also keeps the whole file off the server's heap.
    """
    root = root_state.get_active_root()
    if not root:
        return redirect(url_for("browse.browse"))
    settings = settings_store.get_settings()
    user_id = users.current_user_id()
    run_id = request.args.get("fetch_run") or None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # The name carries the scope: the two files are easy to confuse once they
    # are both sitting in a downloads folder.
    scope = run_id if run_id else (os.path.basename(os.path.normpath(root)) or "claims")
    filename = f"claim_report_{scope}_{stamp}.csv"

    # stream_with_context, not a bare generator: the rows are produced *after*
    # the view returns, by which time the application context - and with it the
    # database connection every row needs - would otherwise have been torn down.
    response = current_app.response_class(
        stream_with_context(claim_export.stream_csv(root, settings, user_id, run_id=run_id)),
        mimetype="text/csv")
    response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    # Nothing downstream can know the length up front, and a proxy buffering
    # this to work one out would undo the streaming.
    response.headers["X-Accel-Buffering"] = "no"
    return response


def _quality_summary(cached_result, settings):
    """Roll a cached document's per-page tags up into one summary. Always a
    dict (mostly-False) even for cache entries that predate a given check -
    classify.py's raw-field lookups default safely to "not flagged" rather
    than erroring, so this just quietly shows no tag until reprocessed."""
    pages = cached_result.get("pages", [])
    if not pages:
        return None
    classified = [classify.classify_page(p, settings) for p in pages]
    types = {c["content_type"] for c in classified if c["content_type"]}
    content_type = next(iter(types)) if len(types) == 1 else ("mixed" if types else None)
    return {
        "is_blurry": any(c["is_blurry"] for c in classified),
        "content_type": content_type,
        "too_clean_suspected": any(c["too_clean_suspected"] for c in classified),
        "cropped_suspected": any(c["cropped_suspected"] for c in classified),
        "correction_suspected": any(c["correction_suspected"] for c in classified),
        "has_face": any(c["has_face"] for c in classified),
    }


def _signature_summary(cached_result, settings):
    """Roll a cached document's signature-forensics tags up into one
    summary: {paste_suspicious, paste_notes, duplicate_count}, or None if
    nothing is flagged (nothing to show)."""
    signatures = [s for p in cached_result.get("pages", []) for s in p.get("signatures", [])]
    if not signatures:
        return None
    classified = [classify.classify_signature(s, settings) for s in signatures]
    paste_suspicious = any(c["paste_suspicious"] for c in classified)
    duplicate_count = sum(1 for c in classified if c["possible_duplicate_of"])
    if not paste_suspicious and not duplicate_count:
        return None
    paste_notes = []
    for c in classified:
        for note in c["notes"]:
            if note not in paste_notes:
                paste_notes.append(note)
    return {"paste_suspicious": paste_suspicious, "paste_notes": paste_notes,
            "duplicate_count": duplicate_count}


def _annotated_docs(claim_path, settings, profile=None):
    docs = claim_scanner.scan_claim_cached(claim_path)
    for doc in docs:
        cached_result = doc["cached_result"]
        doc["cached"] = cached_result is not None
        doc["quality"] = _quality_summary(cached_result, settings) if cached_result else None
        doc["signature_flags"] = _signature_summary(cached_result, settings) if cached_result else None
        # Whether the current Textract profile asks for something this cached
        # analysis cannot contain (see textract_profile.py). Only ever true when
        # re-running would actually add data.
        missing = (textract_profile.missing_from_cache(cached_result, profile)
                   if cached_result and profile else {"features": [], "queries": [], "parser_gains": []})
        doc["profile_missing"] = missing
        doc["profile_stale"] = bool(missing["features"] or missing["queries"])
    return docs


def _profile_notice(docs, profile):
    """One summary of what a reprocess would add across the claim, or None."""
    stale = [doc for doc in docs if doc.get("profile_stale")]
    if not stale:
        return None
    features, queries, parser_gains = set(), set(), set()
    for doc in stale:
        features.update(doc["profile_missing"]["features"])
        queries.update(doc["profile_missing"]["queries"])
        parser_gains.update(doc["profile_missing"].get("parser_gains") or [])
    return {
        "documents": len(stale),
        "unique_documents": len({doc["file_hash"] for doc in stale}),
        "description": textract_profile.describe_missing(
            {"features": sorted(features), "queries": sorted(queries),
             "parser_gains": sorted(parser_gains)}),
    }


def _group_duplicate_docs(docs):
    """Collapse docs sharing identical content (same file_hash) into one
    display row each - two files are only ever different Textract runs if
    their bytes actually differ, so grouping by hash (not filename) never
    hides a genuinely distinct document. Returns a list of:
    {doc_id, file_name_display, path_lines, cached, group, ext, file_hash}
    where doc_id is the first member's doc_id (safe to use for preview/open
    links since identical content means identical cached results/images).
    """
    order = []
    members_by_hash = {}
    for doc in docs:
        h = doc["file_hash"]
        if h not in members_by_hash:
            members_by_hash[h] = []
            order.append(h)
        members_by_hash[h].append(doc)

    groups = []
    for h in order:
        members = members_by_hash[h]
        first = members[0]
        paths_by_name = {}
        for m in members:
            paths_by_name.setdefault(m["file_name"], []).append(m["dir_path"])
        unique_names = list(paths_by_name.keys())

        if len(unique_names) == 1:
            path_lines = [p for p in paths_by_name[unique_names[0]] if p]
        else:
            path_lines = [" | ".join(paths_by_name[n][0] for n in unique_names)]

        groups.append({
            "doc_id": first["doc_id"],
            "file_hash": h,
            "file_name_display": " | ".join(unique_names),
            "path_lines": path_lines,
            "cached": first["cached"],
            "group": first["group"],
            "ext": first["ext"],
            "quality": first["quality"],
            "signature_flags": first["signature_flags"],
        })
    return groups


@bp.route("/claims/<claim_id>")
def claim_detail(claim_id):
    try:
        claim_path = root_state.get_claim_path(claim_id)
    except (ValueError, FileNotFoundError):
        return redirect(url_for("claims.list_claims_view"))

    settings = settings_store.get_settings()
    profile = textract_profile.current_profile(
        settings, current_app.config["DEFAULT_QUERIES"])
    docs = _annotated_docs(claim_path, settings, profile)
    doc_groups = _group_duplicate_docs(docs)

    # One read of the mirrored extraction feeds both panels below.
    extracted = claim_extract.get(claim_id)
    bundle_summary = (extracted or {}).get("claim_bundle_summary") or []
    summary = claim_summary.build_claim_summary(
        claim_id, docs, csv_data.get_claim_row(claim_id, users.current_user_id()),
        bundle_row=bundle_summary[0] if bundle_summary else None,
    )
    amounts = claim_amounts.build_amounts_panel(extracted)
    discharge_summary = claim_sections.build_discharge_summary(docs, settings)
    return render_template("claim_detail.html", claim_id=claim_id, doc_groups=doc_groups,
                           summary=summary, amounts=amounts,
                           discharge_summary=discharge_summary,
                           has_sections=claim_sections.has_any_sections(docs),
                           profile_notice=_profile_notice(docs, profile))


@bp.route("/api/claims/<claim_id>/documents")
def api_claim_documents(claim_id):
    try:
        claim_path = root_state.get_claim_path(claim_id)
    except (ValueError, FileNotFoundError):
        return jsonify({"error": "Claim not found"}), 404

    settings = settings_store.get_settings()
    profile = textract_profile.current_profile(
        settings, current_app.config["DEFAULT_QUERIES"])
    docs = _annotated_docs(claim_path, settings, profile)
    for doc in docs:
        doc.pop("abs_path", None)
        doc.pop("cached_result", None)
    return jsonify({"claim_id": claim_id, "documents": docs})


@bp.route("/api/claims/<claim_id>/rules")
def api_claim_rules(claim_id):
    try:
        claim_path = root_state.get_claim_path(claim_id)
    except (ValueError, FileNotFoundError):
        return jsonify({"error": "Claim not found"}), 404

    return jsonify({"claim_id": claim_id, "results": rules_engine.evaluate_rules(claim_id, claim_path)})
