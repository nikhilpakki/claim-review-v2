"""Local mirror of the eight datasets the pipeline extracts from a claim bundle.

The fetch already parses every JSON in the bundle and builds these datasets in
memory on its way to Redshift; this keeps a copy in reviews.db as it goes. The
review side then has the claim's declared documents, diagnoses, treatments and
amounts without a warehouse round trip per claim page - which also means it
still works for an extract-only fetch (Redshift load unticked) and on a machine
that cannot reach the warehouse at all.

Stored zlib-compressed as one blob per claim. Measured on real bundles, the
datasets are ~220 KB of JSON per claim but ~8 KB compressed - they are mostly
repeated payer/provider snapshots of the same values, so they compress ~27x.
At 100 claims a day that is the difference between ~8 GB and ~300 MB a year in
the same SQLite file the claims list reads on every page load.

Note on load status: the mirror is written when the claim is extracted, before
the Redshift load runs, so `redshift_load_status` inside the mirrored summary
row reads PENDING. The authoritative per-claim load outcome is in
fetch_run_claims.
"""
import json
import zlib
from datetime import datetime, timezone

from .db import get_db

COMPRESSION_LEVEL = 6


def put(claim_id, run_id, datasets):
    """Mirror one claim's datasets. `datasets` is {table_name: [row, ...]} as
    built by the pipeline (already reduced/aggregated, i.e. the same shape that
    goes to Redshift)."""
    payload = json.dumps(datasets, default=str).encode("utf-8")
    blob = zlib.compress(payload, COMPRESSION_LEVEL)
    counts = {name: len(rows) for name, rows in datasets.items()}
    db = get_db()
    db.execute(
        "INSERT INTO claim_extract (registration_id, run_id, extracted_at, row_counts_json, data_zlib) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(registration_id) DO UPDATE SET run_id=excluded.run_id, "
        "extracted_at=excluded.extracted_at, row_counts_json=excluded.row_counts_json, "
        "data_zlib=excluded.data_zlib",
        (claim_id, run_id, datetime.now(timezone.utc).isoformat(),
         json.dumps(counts), blob),
    )
    db.commit()
    return len(blob)


def get(claim_id):
    """All datasets for a claim, or None if it was never fetched through the
    app (e.g. the folder was copied in, or downloaded by the standalone CLI)."""
    row = get_db().execute(
        "SELECT data_zlib FROM claim_extract WHERE registration_id=?", (claim_id,)
    ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(zlib.decompress(row["data_zlib"]).decode("utf-8"))
    except (zlib.error, json.JSONDecodeError, UnicodeDecodeError):
        # Treated as "no mirror" rather than an error: everything that reads
        # this degrades to its pre-mirror behaviour, and a re-fetch rewrites it.
        return None


def get_dataset(claim_id, name):
    """One dataset (e.g. 'claim_bundle_documents'); [] when unavailable, so
    callers can treat "no mirror" and "no rows" the same way when that is the
    sensible reading."""
    data = get(claim_id)
    if not data:
        return []
    return data.get(name, [])


def info(claim_id):
    """Provenance without decompressing: {run_id, extracted_at, row_counts}."""
    row = get_db().execute(
        "SELECT run_id, extracted_at, row_counts_json FROM claim_extract WHERE registration_id=?",
        (claim_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        counts = json.loads(row["row_counts_json"])
    except (json.JSONDecodeError, TypeError):
        counts = {}
    return {"run_id": row["run_id"], "extracted_at": row["extracted_at"], "row_counts": counts}


def versions():
    """{claim_id: extracted_at} for every mirrored claim, in one query.

    The claims list folds this into its rollup cache key, so re-fetching a
    claim invalidates any rule that reads the extracted data even when not a
    single document on disk changed."""
    return {
        row["registration_id"]: row["extracted_at"]
        for row in get_db().execute(
            "SELECT registration_id, extracted_at FROM claim_extract"
        )
    }


def delete(claim_id):
    db = get_db()
    db.execute("DELETE FROM claim_extract WHERE registration_id=?", (claim_id,))
    db.commit()
