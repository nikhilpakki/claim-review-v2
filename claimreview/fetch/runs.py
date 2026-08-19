"""Durable record of claim-fetch runs (SQLite).

fetch_progress.py holds the live state of the run currently in flight; this
holds what survives it. The claims list reads `run_claim_ids()` to pre-select
the claims a finished run landed, which is the whole point of recording them.

Every function here needs a Flask app context (it uses db.get_db()). The fetch
worker thread gets one from `with app.app_context():`, which also gives it its
own SQLite connection - sqlite3 connections cannot be shared across threads.
"""
import json
from datetime import datetime, timezone

from ..db import get_db


def _now():
    return datetime.now(timezone.utc).isoformat()


def create_run(run_id, destination, params):
    db = get_db()
    db.execute(
        "INSERT INTO fetch_runs (run_id, started_at, status, destination, params_json) "
        "VALUES (?, ?, 'running', ?, ?)",
        (run_id, _now(), str(destination), json.dumps(params, default=str)),
    )
    db.commit()


def record_selection(run_id, claim_ids, source):
    """Write the run's claim rows up front, so a run that dies mid-way still
    shows which claims it meant to fetch."""
    db = get_db()
    db.execute(
        "UPDATE fetch_runs SET claims_total=?, source=? WHERE run_id=?",
        (len(claim_ids), source, run_id),
    )
    db.executemany(
        "INSERT OR REPLACE INTO fetch_run_claims "
        "(run_id, registration_id, download_status, extraction_status, load_status) "
        "VALUES (?, ?, 'pending', 'pending', 'pending')",
        [(run_id, claim_id) for claim_id in claim_ids],
    )
    db.commit()


def record_claim(run_id, registration_id, download_status, extraction_status, error=None):
    db = get_db()
    db.execute(
        "INSERT INTO fetch_run_claims "
        "(run_id, registration_id, download_status, extraction_status, load_status, error) "
        "VALUES (?, ?, ?, ?, 'pending', ?) "
        "ON CONFLICT(run_id, registration_id) DO UPDATE SET "
        "download_status=excluded.download_status, "
        "extraction_status=excluded.extraction_status, error=excluded.error",
        (run_id, registration_id, download_status, extraction_status, error),
    )
    db.commit()


def record_report_rows(run_id, report_rows):
    """Fold the pipeline's final per-claim report into the claim rows - this is
    where load_status stops being 'pending'."""
    db = get_db()
    db.executemany(
        "INSERT INTO fetch_run_claims "
        "(run_id, registration_id, download_status, extraction_status, load_status, error) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(run_id, registration_id) DO UPDATE SET "
        "download_status=excluded.download_status, "
        "extraction_status=excluded.extraction_status, "
        "load_status=excluded.load_status, error=excluded.error",
        [
            (
                run_id,
                row["registration_id"],
                row.get("download_status"),
                row.get("extraction_status"),
                row.get("redshift_load_status"),
                row.get("error"),
            )
            for row in report_rows
        ],
    )
    db.commit()


def finish_run(run_id, status, result=None, error=None):
    result = result or {}
    db = get_db()
    db.execute(
        "UPDATE fetch_runs SET finished_at=?, status=?, claims_total=?, claims_ok=?, "
        "claims_failed=?, json_report=?, xlsx_report=?, error=? WHERE run_id=?",
        (
            _now(),
            status,
            result.get("claims_total", 0),
            result.get("claims_ok", 0),
            result.get("claims_failed", 0),
            result.get("json_report"),
            result.get("xlsx_report"),
            error,
            run_id,
        ),
    )
    db.commit()


def mark_bundles_deleted(run_id):
    db = get_db()
    db.execute("UPDATE fetch_runs SET bundles_deleted_at=? WHERE run_id=?", (_now(), run_id))
    db.commit()


def get_run(run_id):
    row = get_db().execute(
        "SELECT * FROM fetch_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    return dict(row) if row else None


def list_runs(limit=20):
    rows = get_db().execute(
        "SELECT * FROM fetch_runs ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(row) for row in rows]


def run_claims(run_id):
    rows = get_db().execute(
        "SELECT * FROM fetch_run_claims WHERE run_id=? ORDER BY registration_id",
        (run_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def run_claim_ids(run_id, successful_only=False):
    """The claim IDs a run landed. `successful_only` keeps the ones that
    actually produced a bundle - a claim whose download failed has no folder on
    disk, so pre-selecting it for review would just be noise."""
    query = "SELECT registration_id FROM fetch_run_claims WHERE run_id=?"
    params = [run_id]
    if successful_only:
        query += " AND extraction_status IN ('SUCCESS','PARTIAL')"
    query += " ORDER BY registration_id"
    return [row["registration_id"] for row in get_db().execute(query, params).fetchall()]


def claim_run_map():
    """{registration_id: {run_id, started_at, extraction_status, run_ids}} - the
    most recent run that fetched each claim, plus every run it appeared in.

    A claim can appear in several runs (re-fetching is idempotent by design).
    The label shows the latest, but filtering matches `run_ids` - selecting an
    earlier run should still show a claim that a later run happened to refresh.
    """
    rows = get_db().execute(
        "SELECT c.registration_id, c.run_id, c.extraction_status, r.started_at "
        "FROM fetch_run_claims c JOIN fetch_runs r ON r.run_id = c.run_id "
        "ORDER BY r.started_at"
    ).fetchall()
    latest = {}
    for row in rows:
        claim_id = row["registration_id"]
        entry = latest.setdefault(claim_id, {"run_ids": []})
        if row["run_id"] not in entry["run_ids"]:
            entry["run_ids"].append(row["run_id"])
        # Ordered oldest-first, so the last write per claim is the newest run -
        # that is the one whose download is on disk now, and what gets labelled.
        entry["run_id"] = row["run_id"]
        entry["started_at"] = row["started_at"]
        entry["extraction_status"] = row["extraction_status"]
    return latest


def runs_with_claims(limit=25):
    """Recent runs that actually landed claims, for the claims-list filter."""
    rows = get_db().execute(
        "SELECT r.run_id, r.started_at, r.status, r.destination, r.source, "
        "       COUNT(c.registration_id) AS claim_count "
        "FROM fetch_runs r JOIN fetch_run_claims c ON c.run_id = r.run_id "
        "GROUP BY r.run_id, r.started_at, r.status, r.destination, r.source "
        "ORDER BY r.started_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def mark_interrupted_runs():
    """Any run still marked 'running' at startup died with the process (the
    worker thread is a daemon). Record that instead of leaving a run that
    polls forever."""
    db = get_db()
    db.execute(
        "UPDATE fetch_runs SET status='failed', finished_at=?, "
        "error=COALESCE(error, 'Interrupted: the app stopped while this run was in progress') "
        "WHERE status='running'",
        (_now(),),
    )
    db.commit()
