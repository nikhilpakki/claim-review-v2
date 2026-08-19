import sqlite3

from flask import current_app, g

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_id TEXT NOT NULL,
  document_path TEXT,
  status TEXT NOT NULL CHECK(status IN ('approved','flagged','rejected')),
  notes TEXT,
  reviewer TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reviews_claim ON reviews(claim_id);
CREATE INDEX IF NOT EXISTS idx_reviews_claim_doc ON reviews(claim_id, document_path);

CREATE TABLE IF NOT EXISTS processing_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  total_files INTEGER,
  processed_files INTEGER,
  cached_files INTEGER,
  failed_files INTEGER,
  status TEXT CHECK(status IN ('running','completed','failed'))
);
CREATE INDEX IF NOT EXISTS idx_runs_claim ON processing_runs(claim_id);

CREATE TABLE IF NOT EXISTS signature_index (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  claim_id TEXT NOT NULL,
  document_path TEXT NOT NULL,
  file_hash TEXT NOT NULL,
  page_number INTEGER NOT NULL,
  signature_id TEXT NOT NULL,
  phash TEXT NOT NULL,
  bbox_json TEXT NOT NULL,
  confidence REAL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_sig_phash ON signature_index(phash);
CREATE INDEX IF NOT EXISTS idx_sig_file_hash ON signature_index(file_hash);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS csv_claims_data (
  registration_id TEXT PRIMARY KEY,
  row_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS csv_upload_meta (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  filename TEXT NOT NULL,
  uploaded_at TEXT NOT NULL,
  row_count INTEGER NOT NULL,
  columns_json TEXT NOT NULL
);

-- Local mirror of the eight datasets the fetch extracts per claim, stored
-- zlib-compressed; see claim_extract.py.
CREATE TABLE IF NOT EXISTS claim_extract (
  registration_id TEXT PRIMARY KEY,
  run_id TEXT,
  extracted_at TEXT NOT NULL,
  row_counts_json TEXT NOT NULL,
  data_zlib BLOB NOT NULL
);

-- Memoized claims-list rollup per claim; see rollup_cache.py for what the
-- key covers. Pure cache: deleting any row only costs a recompute.
CREATE TABLE IF NOT EXISTS claim_rollup_cache (
  claim_id TEXT PRIMARY KEY,
  cache_key TEXT NOT NULL,
  rollup_json TEXT NOT NULL,
  computed_at TEXT NOT NULL
);

-- One claim-fetch run (the /fetch page, or a CLI run recorded by the app).
-- Kept in SQLite rather than only in memory so a run's outcome - and the exact
-- set of claims it landed - survives a page reload or an app restart, which is
-- what the claims list needs to pre-select a finished run's claims.
CREATE TABLE IF NOT EXISTS fetch_runs (
  run_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled')),
  destination TEXT NOT NULL,
  params_json TEXT NOT NULL,
  source TEXT,
  claims_total INTEGER NOT NULL DEFAULT 0,
  claims_ok INTEGER NOT NULL DEFAULT 0,
  claims_failed INTEGER NOT NULL DEFAULT 0,
  json_report TEXT,
  xlsx_report TEXT,
  error TEXT,
  bundles_deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS fetch_run_claims (
  run_id TEXT NOT NULL,
  registration_id TEXT NOT NULL,
  download_status TEXT,
  extraction_status TEXT,
  load_status TEXT,
  error TEXT,
  PRIMARY KEY (run_id, registration_id)
);
CREATE INDEX IF NOT EXISTS idx_fetch_run_claims_run ON fetch_run_claims(run_id);

-- rule_type deliberately carries no CHECK constraint: the valid set is
-- rules_engine.RULE_TYPES, which grows as rule types are added, and both
-- create_rule() and update_rule() reject anything outside it. A CHECK here
-- would mean rebuilding the table (SQLite cannot alter one) for every new
-- rule type, which is exactly what the migration below had to undo once.
CREATE TABLE IF NOT EXISTS rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL,
  rule_type TEXT NOT NULL,
  config_json TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Columns added after a table's initial release - CREATE TABLE IF NOT EXISTS
# above won't add these to an already-existing table, so they're migrated in
# explicitly.
_MIGRATED_COLUMNS = {
    "processing_runs": {
        "blurry_pages": "INTEGER DEFAULT 0",
        "photo_pages": "INTEGER DEFAULT 0",
        "document_pages": "INTEGER DEFAULT 0",
        "unique_documents": "INTEGER DEFAULT 0",
        "suspicious_signatures": "INTEGER DEFAULT 0",
    },
    "fetch_runs": {
        "bundles_deleted_at": "TEXT",
    },
}


def _drop_rules_type_check(conn):
    """Existing databases have a rules.rule_type CHECK listing only the three
    original rule types, so inserting a new type fails with an IntegrityError.
    SQLite cannot drop a constraint, so the table is rebuilt once, preserving
    every row and its id."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='rules'"
    ).fetchone()
    if not row or "CHECK(rule_type IN" not in (row[0] or ""):
        return

    conn.execute("""
        CREATE TABLE rules_rebuilt (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          rule_type TEXT NOT NULL,
          config_json TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "INSERT INTO rules_rebuilt (id, name, rule_type, config_json, enabled, created_at) "
        "SELECT id, name, rule_type, config_json, enabled, created_at FROM rules"
    )
    conn.execute("DROP TABLE rules")
    conn.execute("ALTER TABLE rules_rebuilt RENAME TO rules")


def _migrate(conn):
    _drop_rules_type_check(conn)
    for table, columns in _MIGRATED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, ddl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE_PATH"])
        g.db.row_factory = sqlite3.Row
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def get_latest_runs():
    """{claim_id: row} for the most recent processing_runs row per claim."""
    db = get_db()
    rows = db.execute("""
        SELECT pr.* FROM processing_runs pr
        JOIN (SELECT claim_id, MAX(id) AS max_id FROM processing_runs GROUP BY claim_id) latest
        ON pr.id = latest.max_id
    """).fetchall()
    return {row["claim_id"]: dict(row) for row in rows}


def init_db(app):
    with app.app_context():
        conn = sqlite3.connect(app.config["DATABASE_PATH"])
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        conn.close()
    app.teardown_appcontext(close_db)
