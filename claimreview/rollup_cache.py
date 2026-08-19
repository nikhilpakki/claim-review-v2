"""Cache for the claims-list badges and rule results.

The claims list computes each claim's quality rollup and rule outcomes live,
against *current* settings and rules - that is the "store raw, classify live"
design, and it is what lets a threshold change take effect with no
reprocessing. The cost is that every page load re-reads every cached Textract
result off disk, re-classifies every page and re-evaluates every rule, for
every claim in the folder. With claims accumulating from repeated fetches,
that grows without bound.

This keeps the live semantics and removes the repeated work: the rollup is
recomputed only when something it depends on actually changed. The cache key
covers every input:

- the claim's own content, as the sorted set of its document hashes (a new,
  changed, renamed or deleted file changes the set),
- the settings, since they decide every tag,
- the rules, since rule outcomes are part of the rollup,
- the claims dataset version, since rules cross-check against it,
- the mirrored extraction for this claim, since rules can read it,
- ROLLUP_VERSION, bumped by hand when the classification code itself changes
  in a way that alters results.

Anything that is not in that key must not affect the rollup - if you add an
input to _claim_rollup(), add it here too, or the list will show stale badges.
"""
import hashlib
import json
from datetime import datetime, timezone

from .db import get_db

# Bump when classify.py / rules_engine.py change in a way that alters results
# for unchanged inputs. Cheap: it just forces one recompute per claim.
#
# 2: rules_engine gained the documents_present evaluator and moved date parsing
#    to claim_summary.parse_date_text (ISO dates were being read day-first).
#    Neither changes the rules table, so nothing else in the key would have
#    noticed, and already-cached claims would have kept the old outcomes.
ROLLUP_VERSION = 2


def _digest(*parts):
    h = hashlib.sha256()
    for part in parts:
        h.update(repr(part).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _rules_signature():
    rows = get_db().execute(
        "SELECT id, rule_type, config_json, enabled FROM rules ORDER BY id"
    ).fetchall()
    return [tuple(row) for row in rows]


def _csv_signature():
    """Version of the claims dataset, not its contents: every write path
    (manual upload and fetch upsert) updates csv_upload_meta, so this changes
    whenever any row could have changed."""
    row = get_db().execute(
        "SELECT filename, uploaded_at, row_count FROM csv_upload_meta WHERE id=1"
    ).fetchone()
    return tuple(row) if row else None


def global_fingerprint(settings):
    """The part of the key shared by every claim on the page - computed once
    per request, not once per claim."""
    return _digest(
        ROLLUP_VERSION,
        sorted(settings.items()),
        _rules_signature(),
        _csv_signature(),
    )


def claim_key(global_fp, doc_hashes, extract_version=None):
    """`doc_hashes` is the claim's document content hashes (order-insensitive);
    `extract_version` identifies the mirrored extraction, so re-fetching a
    claim invalidates rules that read it even when no document changed."""
    return _digest(global_fp, sorted(set(doc_hashes)), extract_version)


def get_all():
    """{claim_id: (cache_key, rollup)} for every cached claim, read in one
    query - the claims list needs most of them, and one round trip beats one
    per claim."""
    rows = get_db().execute(
        "SELECT claim_id, cache_key, rollup_json FROM claim_rollup_cache"
    ).fetchall()
    out = {}
    for row in rows:
        try:
            out[row["claim_id"]] = (row["cache_key"], json.loads(row["rollup_json"]))
        except (json.JSONDecodeError, TypeError):
            continue  # corrupt row behaves as a miss and is overwritten on write
    return out


def put_many(entries):
    """entries: [(claim_id, cache_key, rollup)]. One row per claim - a new key
    replaces the old, so this table stays the size of the folder."""
    if not entries:
        return
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    db.executemany(
        "INSERT INTO claim_rollup_cache (claim_id, cache_key, rollup_json, computed_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(claim_id) DO UPDATE SET cache_key=excluded.cache_key, "
        "rollup_json=excluded.rollup_json, computed_at=excluded.computed_at",
        [(claim_id, key, json.dumps(rollup), now) for claim_id, key, rollup in entries],
    )
    db.commit()


def invalidate(claim_id):
    db = get_db()
    db.execute("DELETE FROM claim_rollup_cache WHERE claim_id=?", (claim_id,))
    db.commit()


def clear():
    db = get_db()
    db.execute("DELETE FROM claim_rollup_cache")
    db.commit()
