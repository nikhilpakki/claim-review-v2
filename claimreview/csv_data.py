"""Stores the claims-management CSV/Excel export (e.g. a PMJAY-style
`claims_paid_t.csv`) that rules cross-check documents against. One dataset
is active at a time - uploading a new file replaces the previous one
entirely. Rows are matched to a claim by the `registration_id` column,
which is the same value as the claim folder name used everywhere else in
this app.
"""
import csv
import io
import json
from datetime import datetime, timezone

from .db import get_db


class CsvUploadError(Exception):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def upload_csv(file_storage):
    """file_storage: a werkzeug FileStorage from request.files. Replaces
    whatever claims dataset was previously loaded. Returns {row_count, columns}."""
    raw = file_storage.read()
    text = raw.decode("utf-8-sig", errors="replace")  # -sig strips an Excel-added BOM
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "registration_id" not in reader.fieldnames:
        raise CsvUploadError("CSV must have a 'registration_id' column - "
                              "that's how a row is matched to a claim folder.")

    columns = reader.fieldnames
    rows = []
    for row in reader:
        reg_id = (row.get("registration_id") or "").strip()
        if reg_id:
            rows.append((reg_id, row))

    db = get_db()
    db.execute("DELETE FROM csv_claims_data")
    for reg_id, row in rows:
        db.execute(
            "INSERT OR REPLACE INTO csv_claims_data (registration_id, row_json) VALUES (?, ?)",
            (reg_id, json.dumps(row)),
        )
    db.execute(
        "INSERT INTO csv_upload_meta (id, filename, uploaded_at, row_count, columns_json) "
        "VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET filename=excluded.filename, uploaded_at=excluded.uploaded_at, "
        "row_count=excluded.row_count, columns_json=excluded.columns_json",
        (file_storage.filename or "upload.csv", _now(), len(rows), json.dumps(columns)),
    )
    db.commit()
    return {"row_count": len(rows), "columns": columns}


def upsert_claim_rows(rows, source="fetch"):
    """Merge claim rows fetched straight from the warehouse into the dataset.

    Unlike upload_csv(), this does NOT clear what is already there: a fetch
    brings in one batch of claims, and wiping the dataset would strip the
    metadata off every claim fetched earlier - which the rules and the claim
    summary would then silently stop cross-checking. Rows are replaced per
    registration_id, so re-fetching a claim refreshes it.

    Returns the number of rows written.
    """
    rows = [row for row in rows if (row.get("registration_id") or "").strip()]
    if not rows:
        return 0

    # The rule builder's field list comes from csv_upload_meta.columns; union
    # rather than overwrite, so a rule that references a column only present in
    # a previously uploaded CSV keeps resolving.
    meta = get_upload_meta()
    columns = list(meta["columns"]) if meta else []
    for row in rows:
        for column in row:
            if column not in columns:
                columns.append(column)

    db = get_db()
    db.executemany(
        "INSERT OR REPLACE INTO csv_claims_data (registration_id, row_json) VALUES (?, ?)",
        [
            ((row.get("registration_id") or "").strip(), json.dumps(row))
            for row in rows
        ],
    )
    total = db.execute("SELECT COUNT(*) FROM csv_claims_data").fetchone()[0]
    db.execute(
        "INSERT INTO csv_upload_meta (id, filename, uploaded_at, row_count, columns_json) "
        "VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET filename=excluded.filename, uploaded_at=excluded.uploaded_at, "
        "row_count=excluded.row_count, columns_json=excluded.columns_json",
        (source, _now(), total, json.dumps(columns)),
    )
    db.commit()
    return len(rows)


def get_claim_row(claim_id):
    """The uploaded CSV row for this claim, as a {column: value} dict, or
    None if no dataset is loaded or it has no row for this claim."""
    db = get_db()
    row = db.execute(
        "SELECT row_json FROM csv_claims_data WHERE registration_id=?", (claim_id,)
    ).fetchone()
    return json.loads(row["row_json"]) if row else None


def get_upload_meta():
    db = get_db()
    row = db.execute(
        "SELECT filename, uploaded_at, row_count, columns_json FROM csv_upload_meta WHERE id=1"
    ).fetchone()
    if not row:
        return None
    return {"filename": row["filename"], "uploaded_at": row["uploaded_at"],
            "row_count": row["row_count"], "columns": json.loads(row["columns_json"])}


def get_available_fields():
    """Column names from the currently loaded dataset, for the rule
    builder's autocomplete - empty list (not an error) if nothing's loaded,
    since rules can be configured before any CSV is uploaded."""
    meta = get_upload_meta()
    return meta["columns"] if meta else []


def clear_csv_data():
    db = get_db()
    db.execute("DELETE FROM csv_claims_data")
    db.execute("DELETE FROM csv_upload_meta")
    db.commit()
