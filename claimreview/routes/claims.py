import os
from datetime import datetime

from flask import (Blueprint, current_app, jsonify, redirect, render_template,
                   request, stream_with_context, url_for)

from .. import (claim_amounts, claim_export, claim_extract, claim_index, claim_scanner,
                claim_sections, claim_summary, classify, csv_data, doc_tags, processing,
                rollup_cache, root_state, rules_engine, settings_store, textract_profile,
                users)
from ..db import get_latest_runs
from ..fetch import runs as fetch_runs

bp = Blueprint("claims", __name__)


# Both of these moved to claim_index, which is where the background pass
# needs them too; re-exported so nothing that imported them from here breaks.
_empty_rollup = claim_index.empty_rollup
_claim_rollup = claim_index.claim_rollup


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


def _page_params():
    """The filters the page is showing, read from the query string.

    One reader for both the HTML route and the JSON one, so a link pasted
    into the address bar and a click on the page's own paging controls cannot
    produce different lists.
    """
    args = request.args
    try:
        offset = max(0, int(args.get("offset", 0)))
    except (TypeError, ValueError):
        offset = 0
    return {
        "offset": offset,
        "limit": claim_index.PAGE_SIZE,
        "query": (args.get("q") or "").strip(),
        "status_filter": args.get("status") or "all",
        "tags": [t for t in (args.get("tags") or "").split(",") if t],
        "run_filter": args.get("fetch_run") or "",
        "run_only": args.get("run_only") == "1",
    }


def _claims_page(ctx, params, preselect):
    """One page of claims plus the counts the page needs to describe itself."""
    matching, scanned, considered = claim_index.select(
        ctx, query=params["query"], status_filter=params["status_filter"],
        tags=params["tags"], run_filter=params["run_filter"],
        run_only=params["run_only"], preselect=preselect)
    entries = claim_index.page(ctx, matching, params["offset"], params["limit"],
                               preselect=preselect)
    warm = claim_index.warm_state(ctx.root, ctx.user_id)
    return {
        "claims": entries,
        "offset": params["offset"],
        "limit": params["limit"],
        "matching": len(matching),
        "folder_total": len(ctx.claims),
        # How many of the claims that passed the cheap filters have a rollup
        # to judge. A tag filter can only have searched these, and saying so
        # is the difference between "no blurry claims" and "none found yet".
        "scanned": scanned,
        "considered": considered,
        "warming": warm["running"],
        "ready": sum(1 for _claim, state in matching
                     if state["status"] == "completed"),
    }


@bp.route("/claims")
def list_claims_view():
    root = root_state.get_active_root()
    if not root:
        return redirect(url_for("browse.browse"))
    user_id = users.current_user_id()
    ctx = claim_index.Context(root, user_id)
    preselect, fetch_run = _fetch_run_context([c["claim_id"] for c in ctx.claims])
    params = _page_params()
    data = _claims_page(ctx, params, preselect)

    # Started only after the page's own fifteen claims are in hand, so the
    # background pass is never competing with the request the reviewer is
    # waiting on for the same folder.
    claim_index.warm_start(current_app._get_current_object(), root, user_id,
                           ctx.global_fp, len(ctx.claims))

    fetch_run_options = fetch_runs.runs_with_claims(limit=25)
    # Only offer runs whose claims are actually in this folder - a run that
    # downloaded somewhere else would filter the list down to nothing.
    here_counts = {}
    for claim in ctx.claims:
        for run_id in (ctx.claim_runs.get(claim["claim_id"], {}).get("run_ids") or []):
            here_counts[run_id] = here_counts.get(run_id, 0) + 1
    # The count shown is claims from that run *in this folder*, which is what
    # the filter will actually reveal - a run's own total can include claims
    # whose download failed, or that were downloaded somewhere else.
    fetch_run_options = [
        dict(run, claims_here=here_counts[run["run_id"]])
        for run in fetch_run_options if run["run_id"] in here_counts
    ]
    return render_template("claims_list.html", page=data, active_root=root,
                           settings=ctx.settings, fetch_run=fetch_run,
                           fetch_run_options=fetch_run_options,
                           page_size=claim_index.PAGE_SIZE,
                           # Every claim the fetch landed, not just the ones on
                           # this page: "process what I just fetched" was one
                           # click before paging and has to stay one click.
                           preselect_ids=sorted(preselect),
                           selected_run_id=params["run_filter"])


@bp.route("/api/claims/page")
def api_claims_page():
    """One page of the claims list, rendered server-side.

    The rows come back as HTML from the same partial the first page is
    rendered with, rather than as data the browser assembles itself: there is
    one definition of a claim row, and a badge added to it appears on every
    page without a second implementation to keep in step.
    """
    root = root_state.get_active_root()
    if not root:
        return jsonify({"error": "No active root"}), 400
    user_id = users.current_user_id()
    ctx = claim_index.Context(root, user_id)
    preselect, _fetch_run = _fetch_run_context([c["claim_id"] for c in ctx.claims])
    data = _claims_page(ctx, _page_params(), preselect)
    claim_index.warm_start(current_app._get_current_object(), root, user_id,
                           ctx.global_fp, len(ctx.claims))
    return jsonify({
        **{k: v for k, v in data.items() if k != "claims"},
        "rows_html": render_template("partials/claim_rows.html",
                                     claims=data["claims"], settings=ctx.settings),
    })


@bp.route("/api/claims/matching-ids")
def api_matching_ids():
    """Every claim id matching the current filters, for "select all".

    A separate call because it is only wanted when the reviewer asks for it,
    and a thousand ids on every page turn would be a thousand ids nobody
    reads.
    """
    root = root_state.get_active_root()
    if not root:
        return jsonify({"error": "No active root"}), 400
    user_id = users.current_user_id()
    ctx = claim_index.Context(root, user_id)
    preselect, _fetch_run = _fetch_run_context([c["claim_id"] for c in ctx.claims])
    params = _page_params()
    matching, _scanned, _considered = claim_index.select(
        ctx, query=params["query"], status_filter=params["status_filter"],
        tags=params["tags"], run_filter=params["run_filter"],
        run_only=params["run_only"], preselect=preselect)
    return jsonify({"claim_ids": [claim["claim_id"] for claim, _state in matching]})


@bp.route("/api/claims/scan-progress")
def api_scan_progress():
    """How far the background pass has got, for the line under the filters."""
    root = root_state.get_active_root()
    if not root:
        return jsonify({"running": False, "scanned": 0, "total": 0})
    return jsonify({**claim_index.warm_state(root, users.current_user_id()),
                    # One at most, whoever it belongs to. Reported so "why is
                    # the box busy" has an answer that is not a guess.
                    "passes_running": claim_index.active_passes()})


@bp.route("/claims/export.csv", methods=["GET", "POST"])
def export_claims_csv():
    """The fully processed claims in the active folder, as one CSV row each.

    Three scopes, narrowest first: `claim_ids` (the rows ticked on the page,
    POSTed because there can be thousands of them), `fetch_run` (one batch), or
    neither, meaning the whole folder. A folder accumulates every fetch ever run
    into it, so exporting all of it is rarely what somebody reviewing today's
    batch wants.

    Streamed. A folder of a few thousand claims means re-reading every cached
    document, which takes long enough that a buffered response would look like
    a hung tab; streaming also keeps the whole file off the server's heap.
    """
    root = root_state.get_active_root()
    if not root:
        return redirect(url_for("browse.browse"))
    settings = settings_store.get_settings()
    user_id = users.current_user_id()
    run_id = request.values.get("fetch_run") or None
    # Ticked rows arrive by POST: a folder can hold thousands of claims and a
    # query string cannot carry that many ids.
    claim_ids = [c for c in (request.form.get("claim_ids") or "").split(",") if c.strip()]
    claim_ids = [c.strip() for c in claim_ids] or None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # The name carries the scope: these files are easy to confuse once several
    # are sitting in a downloads folder.
    if claim_ids:
        scope = f"selection_{len(claim_ids)}"
    elif run_id:
        scope = run_id
    else:
        scope = os.path.basename(os.path.normpath(root)) or "claims"
    filename = f"claim_report_{scope}_{stamp}.csv"

    # stream_with_context, not a bare generator: the rows are produced *after*
    # the view returns, by which time the application context - and with it the
    # database connection every row needs - would otherwise have been torn down.
    response = current_app.response_class(
        stream_with_context(claim_export.stream_csv(root, settings, user_id, run_id=run_id,
                                                    claim_ids=claim_ids)),
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


def _document_tags(cached_result, patterns, by_hash, file_hash):
    """The admin-defined tags this document carries, across all of its pages.

    Keyed by content hash, because a claim routinely holds the same file under
    several paths and they cannot carry different tags.
    """
    if not patterns or not cached_result:
        return []
    if file_hash not in by_hash:
        found = set()
        for page in cached_result.get("pages", []):
            found |= doc_tags.tags_for_page(page, patterns)
        by_hash[file_hash] = sorted(found)
    return by_hash[file_hash]


def _annotated_docs(claim_path, settings, profile=None):
    docs = claim_scanner.scan_claim_cached(claim_path)
    # Prepared once for the whole claim: compiling a pattern per page of a
    # couple of hundred would dwarf the matching.
    tag_patterns = doc_tags.compiled()
    tags_by_hash = {}
    for doc in docs:
        cached_result = doc["cached_result"]
        doc["cached"] = cached_result is not None
        doc["quality"] = _quality_summary(cached_result, settings) if cached_result else None
        doc["doc_tags"] = _document_tags(cached_result, tag_patterns, tags_by_hash,
                                         doc["file_hash"])
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
            "doc_tags": first["doc_tags"],
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
