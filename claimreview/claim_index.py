"""The claims list, one page at a time, and the pass that fills in the rest.

A folder of a thousand claims took about fifteen minutes to render. The
badges were cached - that part was already solved - but the page still had to
prove the cache was still valid, and proving it means hashing every document
of every claim. hash_file memoizes by (mtime, size), so the bytes are usually
not re-read, but an os.walk of the folder plus an os.stat per file, a
thousand times over, is minutes on its own against a synced network folder.

Nothing here makes that work faster. It makes it smaller, and moves what is
left off the path the reviewer is waiting on:

  - A *snapshot* of the folder is cheap: the claim ids are directory names
    and the processing status is one query. That is enough to count the
    folder, filter it by id, status or fetch run, and decide which fifteen
    claims to show.
  - Only those fifteen are scanned and rolled up, when they are asked for.
  - A background pass then walks the whole folder doing the same work and
    leaving it in the rollup cache, so later pages are already warm and the
    quality filters have something to filter.

The quality filters are the honest casualty. "Blurry" cannot be answered for
a claim nobody has scanned, so they match over what the background pass has
reached so far and the page says how far that is, rather than quietly
claiming the folder holds no blurry claims.
"""
import threading
import time

from flask import current_app

from . import (claim_extract, claim_scanner, classify, processing, rollup_cache,
               rules_engine, settings_store, users)
from .db import get_latest_runs
from .fetch import runs as fetch_runs

PAGE_SIZE = 15
# How many claims the background pass does between checks that it is still
# wanted, and between writes to the rollup cache. Small enough that abandoning
# a root loses almost no work, large enough that the cache write is not the
# dominant cost.
WARM_BATCH = 10

# Which rollup counter each filter button reads. The page sends the button
# names; keeping the mapping here means a new badge is added in one place.
TAG_FIELDS = {
    "blurry": "blurry_pages",
    "paste": "paste_signatures",
    "duplicate": "duplicate_signatures",
    "tooClean": "too_clean_pages",
    "cropped": "cropped_pages",
    "correction": "correction_pages",
    "hasFace": "face_pages",
    "ruleViolation": "rule_violations",
}


def empty_rollup():
    return {"blurry_pages": 0, "too_clean_pages": 0, "cropped_pages": 0,
            "correction_pages": 0, "paste_signatures": 0, "duplicate_signatures": 0,
            "face_pages": 0, "rule_violations": 0}


def claim_rollup(claim_id, claim_path, docs, settings, user_id=None, rules=None):
    """Live counts for the claims-list badges, computed fresh against
    *current* settings/rules from each already-cached, deduped document -
    not a processing_runs snapshot - so retuning a threshold or rule
    updates these immediately, with no reprocessing or AWS cost. `docs` is
    an already-scanned claim_scanner.scan_claim_cached() list, shared with
    the rules evaluation below so the claim's files are only hashed/read
    once per page load, not once per rollup metric plus once per rule."""
    rollup = empty_rollup()
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


# --------------------------------------------------------------- the snapshot

class Context:
    """Everything a page of claims needs that does not vary per claim.

    Built once per request. Each of these is a single query or a single
    directory read; none of them touches a claim's documents, which is the
    whole point - the folder can be counted, filtered and paged without
    opening anything.
    """

    def __init__(self, root, user_id):
        self.root = root
        self.user_id = user_id
        self.claims = claim_scanner.list_claims(root)
        self.latest_runs = get_latest_runs()
        self.settings = settings_store.get_settings()
        self.claim_runs = fetch_runs.claim_run_map()
        self.rules = rules_engine.list_rules(user_id)
        self.global_fp = rollup_cache.global_fingerprint(self.settings, user_id)
        self.cached = rollup_cache.get_all(user_id)
        self.extract_versions = claim_extract.versions()
        self.by_id = {c["claim_id"]: c for c in self.claims}

    def status(self, claim):
        return processing.get_claim_status(claim["claim_id"],
                                           self.latest_runs.get(claim["claim_id"]))

    def known_rollup(self, claim_id):
        """The cached rollup for a claim, if one was computed under the
        current settings and rules.

        The stored global fingerprint is checked, so retuning a threshold or
        editing a rule empties this immediately and the page goes back to
        saying nothing has been scanned yet - which is true, and better than
        filtering on counts from the old settings.

        What is deliberately *not* checked is whether the claim's documents
        have changed, because that means hashing them, and that is the
        expensive thing this module exists to avoid. The page still does it
        for the fifteen claims it is about to show; this looser answer only
        decides whether a claim can take part in a quality filter, where the
        alternative is not a better answer but no answer at all.
        """
        hit = self.cached.get(claim_id)
        if not hit:
            return None
        _key, global_fp, rollup = hit
        return rollup if global_fp == self.global_fp else None


def _entry(ctx, claim, status, rollup, preselect):
    claim_id = claim["claim_id"]
    return {
        "claim_id": claim_id,
        "path": claim["path"],
        "status": status,
        "rollup": rollup if rollup is not None else empty_rollup(),
        # The page shows an unscanned claim without badges; it must not show
        # it as a claim with nothing wrong, which is the same markup.
        "rollup_known": rollup is not None,
        "preselected": claim_id in preselect,
        "fetch_run": ctx.claim_runs.get(claim_id),
    }


def resolve(ctx, claim, status=None, settings=None, rules=None):
    """One claim's rollup, computed if the cache cannot prove itself.

    This is the expensive path, and it runs for the claims on the page being
    looked at - fifteen of them - or for one claim at a time in the
    background pass. Returns (status, rollup, fresh_cache_entry_or_None),
    where the third is what to write back, or None when the cache was right.
    """
    status = status or ctx.status(claim)
    if status["status"] not in ("completed", "failed"):
        return status, empty_rollup(), None
    docs = claim_scanner.scan_claim_hashed(claim["path"])
    key = rollup_cache.claim_key(ctx.global_fp, (d["file_hash"] for d in docs),
                                 ctx.extract_versions.get(claim["claim_id"]))
    hit = ctx.cached.get(claim["claim_id"])
    if hit and hit[0] == key:
        # The full key already covers the settings and rules, so a row that
        # matches it is current whatever its global_fp column says. A row
        # written before that column existed has '', and would otherwise read
        # as "never scanned" for ever - so it is re-stored as it is found.
        # Nothing is recomputed: this is an UPDATE of one column.
        if hit[1] != ctx.global_fp:
            return status, hit[2], (claim["claim_id"], key, hit[2])
        return status, hit[2], None
    claim_scanner.attach_cached_results(docs)
    rollup = claim_rollup(claim["claim_id"], claim["path"], docs,
                          settings or ctx.settings, ctx.user_id,
                          rules=rules or ctx.rules)
    return status, rollup, (claim["claim_id"], key, rollup)


def select(ctx, query="", status_filter="all", tags=(), run_filter="",
           run_only=False, preselect=()):
    """The claims matching the filters, in folder order, without scanning any
    of them.

    Returns (matching, scanned, considered, searched_all):
      - `matching` is the filtered claim list,
      - `scanned` is how many of the claims that passed the cheap filters
        have a rollup to judge, which is what a tag filter could see,
      - `considered` is how many passed the cheap filters at all,
      - `searched_all` says a fetch-run filter was set aside to search the
        whole folder, which the page has to tell the reviewer.

    The two counts are reported separately so the page can say "3 of 240
    scanned so far" rather than implying it searched the folder.
    """
    query = (query or "").strip().lower()
    # Typing a claim id is a search for that claim, so it searches the folder.
    # Leaving the fetch-run filter on top of it meant the box could only find
    # claims inside whichever batch happened to be selected - and a reviewer
    # who has just picked a batch and then pastes an id from elsewhere gets
    # "no claims match" about a claim that is sitting right there. The filter
    # is set aside rather than cleared, so it comes back when the box does,
    # and the page says this is happening instead of leaving the dropdown
    # looking like it still applies.
    searched_all = bool(query) and bool(run_filter or run_only)
    if query:
        run_filter, run_only = "", False
    tags = [t for t in (tags or []) if t in TAG_FIELDS]
    matching, scanned, considered = [], 0, 0
    for claim in ctx.claims:
        claim_id = claim["claim_id"]
        if query and query not in claim_id.lower():
            continue
        state = ctx.status(claim)
        if status_filter not in ("all", "", None) and state["status"] != status_filter:
            continue
        if run_only and claim_id not in preselect:
            continue
        if run_filter:
            runs = (ctx.claim_runs.get(claim_id) or {}).get("run_ids") or []
            if run_filter not in runs:
                continue
        considered += 1
        rollup = ctx.known_rollup(claim_id)
        is_done = state["status"] in ("completed", "failed")
        # A claim that was never processed has no documents to be blurry, so
        # it counts as scanned: there is nothing pending that could change
        # the answer for it.
        if rollup is not None or not is_done:
            scanned += 1
        if tags:
            if is_done and rollup is None:
                continue  # not scanned yet - cannot say, so does not match
            counts = rollup or empty_rollup()
            if not all(counts.get(TAG_FIELDS[t]) for t in tags):
                continue
        matching.append((claim, state))
    return matching, scanned, considered, searched_all


def page(ctx, matching, offset, limit, preselect=()):
    """Render-ready entries for one window of the matching claims.

    This is where the scanning happens, and only here: `limit` claims, not a
    folder's worth.
    """
    window = matching[offset:offset + limit]
    entries, fresh = [], []
    for claim, state in window:
        status, rollup, new = resolve(ctx, claim, status=state)
        if new:
            fresh.append(new)
        entries.append(_entry(ctx, claim, status, rollup, preselect))
    if fresh:
        rollup_cache.put_many(fresh, ctx.user_id, ctx.global_fp)
        for claim_id, key, rollup in fresh:
            ctx.cached[claim_id] = (key, ctx.global_fp, rollup)
    return entries


# --------------------------------------------------------------- the background pass

_lock = threading.Lock()
_warmers = {}       # (root, user_id) -> state dict
# At most one background pass at a time in the whole process, whoever it
# belongs to. The work is almost entirely disk, so ten reviewers on one folder
# meant ten threads reading it at once - slower for all of them than one
# thread reading it ten times, and competing with the requests they were
# waiting on. A pass that cannot have the slot waits for it rather than
# doubling up.
_warm_slot = threading.Semaphore(1)
# How many passes are past the slot and actually reading the folder, as
# opposed to queued behind it. Reported by the scan-progress endpoint so the
# answer to "why is the box busy" does not have to be guessed at.
_warm_active = 0
# Page requests reading claims off disk right now. The background pass stands
# aside while this is non-zero: it holds the folder-reading slot for minutes
# at a time, and a reviewer picking a fetch run with two claims in it was
# waiting behind two hundred claims of someone else's disk traffic for a page
# that needed two.
_foreground = 0
_foreground_idle = threading.Condition(_lock)
# ...but only for so long. With ten reviewers the box is rarely quiet, and a
# pass that waited for silence would never run at all, leaving the quality
# filters permanently empty. It yields, then carries on.
YIELD_SECONDS = 2.0
# How many of the claims matching the current filter are named to the pass as
# worth reading first. Enough to cover a fetch run and several pages past it;
# the rest of the folder follows in its own order.
PRIORITY_CLAIMS = 100


class foreground:
    """Marks a request a reviewer is sitting in front of.

    Wrapped around the whole of a claims-list request, not just the part that
    reads claims: building the Context queries the database and listing the
    folder reads a directory, and measured, those are most of what the
    reviewer waits for - gating only the scan left the pass competing for
    exactly the stretch that mattered.
    """

    def __enter__(self):
        global _foreground
        with _lock:
            _foreground += 1
        return self

    def __exit__(self, *_exc):
        global _foreground
        with _foreground_idle:
            _foreground = max(0, _foreground - 1)
            if not _foreground:
                _foreground_idle.notify_all()
        return False


def _yield_to_requests(state):
    """Wait, briefly, for the reviewers' own pages to finish reading."""
    if not _foreground:
        return
    deadline = time.monotonic() + YIELD_SECONDS
    with _foreground_idle:
        while _foreground and not state["stop"]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            _foreground_idle.wait(remaining)


def waiting_for_requests():
    """Whether the background pass is currently standing aside."""
    with _lock:
        return _foreground > 0


def active_passes():
    """How many background passes are reading a folder right now. One, at
    most - see _warm_slot."""
    with _lock:
        return _warm_active


def warm_state(root, user_id):
    with _lock:
        state = _warmers.get((root, user_id))
        if not state:
            return {"running": False, "scanned": 0, "total": 0, "started": False}
        return {"running": state["running"], "scanned": state["scanned"],
                "total": state["total"], "started": True,
                "completed": bool(state.get("completed"))}


def warm_start(app, root, user_id, global_fp=None, total=None, first=()):
    """Start the background pass for this root, unless there is no work.

    Skipped when one is already going, and when the last one finished over
    the same folder under the same settings and rules. Without that second
    check every visit to the claims list would walk the whole folder again
    in the background - which is the cost this module exists to remove, just
    moved somewhere the reviewer cannot see it.

    One thread, not a pool. The work is dominated by directory reads on a
    synced folder, which do not overlap usefully (attach_cached_results
    measured threads as a fifth faster at best, and that was for file opens);
    and a second thread would compete with the reviewer's own page for the
    same disk, which is the thing being optimised for.
    """
    with _lock:
        key = (root, user_id)
        state = _warmers.get(key)
        if state and state["running"]:
            # The pass is already going, and the reviewer has just told us
            # what they are looking at - a fetch run, a search, a page. It
            # reads those next instead of finishing the folder first.
            state["first"] = list(first or ())
            return state
        if (state and state.get("completed")
                and state.get("fp") == global_fp and state.get("total") == total):
            return state
        state = {"running": True, "scanned": 0, "total": total or 0, "stop": False,
                 "completed": False, "fp": global_fp, "first": list(first or ())}
        for (other_root, _other_user), other in _warmers.items():
            # Only a pass over a *different folder* is no longer wanted. This
            # compared the whole key, so a second reviewer opening the same
            # folder cancelled the first one's pass - with ten of them nobody's
            # ever finished, and every page load started them all again.
            if other_root != root:
                other["stop"] = True
        _warmers[key] = state
    thread = threading.Thread(target=_warm, args=(app, root, user_id, state),
                             name=f"claim-warm-{user_id}", daemon=True)
    thread.start()
    return state


def _next_claim(ctx, state, done, cursor):
    """The next claim to read, and where folder order has got to.

    Whatever the reviewer is looking at comes first, re-read from the state
    on every claim so that picking a fetch run part-way through the pass
    redirects it rather than joining a queue behind the whole folder. Then
    the folder in its own order, with `cursor` carrying the position so this
    stays one walk rather than one per claim.
    """
    with _lock:
        first = list(state.get("first") or ())
    for claim_id in first:
        if claim_id not in done:
            claim = ctx.by_id.get(claim_id)
            if claim is not None:
                return claim, cursor
    while cursor < len(ctx.claims):
        claim = ctx.claims[cursor]
        cursor += 1
        if claim["claim_id"] not in done:
            return claim, cursor
    return None, cursor


def _warm(app, root, user_id, state):
    """Walk the folder filling the rollup cache. Runs with its own app
    context, so it gets its own SQLite connection (WAL, so it does not block
    the reviewer's reads) and its own hash memo flush on the way out."""
    global _warm_active
    entered = False
    try:
        # Queued, not run alongside: see _warm_slot. Acquired before the app
        # context so a waiting pass is not holding a database connection open
        # for the length of somebody else's pass.
        with _warm_slot, app.app_context():
            entered = True
            with _lock:
                _warm_active += 1
            ctx = Context(root, user_id)
            with _lock:
                state["total"] = len(ctx.claims)
                # Recorded from the thread's own context, not the caller's:
                # settings can change between the request and this starting,
                # and what matters is the fingerprint the pass actually used.
                state["fp"] = ctx.global_fp
            fresh = []
            done, cursor = set(), 0
            while not state["stop"]:
                claim, cursor = _next_claim(ctx, state, done, cursor)
                if claim is None:
                    break
                done.add(claim["claim_id"])
                # Checked here rather than once at the top: a pass runs for
                # minutes, and the reviewers it should defer to arrive during
                # it, not before it.
                _yield_to_requests(state)
                if state["stop"]:
                    break
                try:
                    _status, _rollup, new = resolve(ctx, claim)
                    if new:
                        fresh.append(new)
                except Exception:
                    # One unreadable claim must not stop the folder. It stays
                    # unscanned, and the page says so by counting it out.
                    pass
                with _lock:
                    state["scanned"] += 1
                if len(fresh) >= WARM_BATCH:
                    rollup_cache.put_many(fresh, user_id, ctx.global_fp)
                    fresh = []
            if fresh:
                rollup_cache.put_many(fresh, user_id, ctx.global_fp)
            with _lock:
                # Only a pass that reached the end counts as done; one that
                # was abandoned for another root must run again next time.
                state["completed"] = not state["stop"]
    except Exception:
        pass
    finally:
        with _lock:
            state["running"] = False
            if entered:
                _warm_active = max(0, _warm_active - 1)
