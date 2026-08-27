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

-- The claims dataset. owner_user_id 0 means the shared rows a fetch populates
-- from the warehouse; a row owned by a real user id comes from that user's own
-- CSV upload and shadows the shared one for them alone.
--
-- 0 rather than NULL on purpose: "INTEGER PRIMARY KEY" is a rowid alias in
-- SQLite, so inserting NULL there silently becomes 1 - which would have handed
-- the existing shared dataset to whoever happened to be user 1. NULL also
-- compares as distinct in a UNIQUE index, so it cannot enforce "one shared row".
CREATE TABLE IF NOT EXISTS csv_claims_data (
  owner_user_id INTEGER NOT NULL DEFAULT 0,
  registration_id TEXT NOT NULL,
  row_json TEXT NOT NULL,
  PRIMARY KEY (owner_user_id, registration_id)
);

CREATE TABLE IF NOT EXISTS csv_upload_meta (
  owner_user_id INTEGER NOT NULL PRIMARY KEY DEFAULT 0,
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
-- Keyed per user as well as per claim: rule enablement is a personal choice,
-- so two reviewers looking at the same folder legitimately have different
-- badge counts and must not overwrite each other's cached answer.
CREATE TABLE IF NOT EXISTS claim_rollup_cache (
  user_id INTEGER NOT NULL DEFAULT 0,
  claim_id TEXT NOT NULL,
  cache_key TEXT NOT NULL,
  rollup_json TEXT NOT NULL,
  computed_at TEXT NOT NULL,
  PRIMARY KEY (user_id, claim_id)
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
  bundles_deleted_at TEXT,
  started_by_user_id INTEGER,
  started_by TEXT
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

-- People who use the tool. Rules and detection settings are administered
-- centrally; what varies per user is which rules they have switched on, which
-- folder they are reviewing, and their own claims dataset.
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  password_hash TEXT NOT NULL,
  display_name TEXT,
  is_admin INTEGER NOT NULL DEFAULT 0,
  is_active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  last_login_at TEXT
);

-- A user's override of a rule's enabled state. A rule with no row here follows
-- the rule's own default, so an admin adding a rule reaches everyone without
-- having to touch each account.
CREATE TABLE IF NOT EXISTS user_rule_prefs (
  user_id INTEGER NOT NULL,
  rule_id INTEGER NOT NULL,
  enabled INTEGER NOT NULL,
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (user_id, rule_id)
);

-- Per-user UI state that used to be global (the active review folder lived in
-- one shared last_root.json, so one person changing folders moved everyone).
CREATE TABLE IF NOT EXISTS user_state (
  user_id INTEGER PRIMARY KEY,
  active_root TEXT,
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

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
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  -- Anyone may write a rule and everyone sees all of them, so editing and
  -- deleting are restricted to the author (or an admin) - which needs an
  -- author on the row. Plain INTEGER, never INTEGER PRIMARY KEY: that is a
  -- rowid alias in SQLite and would silently turn NULL into a real id.
  created_by_user_id INTEGER,
  -- The author's name as it was at the time, like fetch_runs.started_by, so a
  -- rule still says who wrote it after that account is gone.
  created_by TEXT
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
        # Who started it: with several people sharing one instance, a blocked
        # user needs to know whose run is in the way.
        "started_by_user_id": "INTEGER",
        "started_by": "TEXT",
    },
    # Rules predating this column were created when only an admin could make
    # one, so leaving them NULL is accurate: they belong to no individual and
    # stay admin-only to edit or delete. See rules_engine.can_modify_rule.
    "rules": {
        "created_by_user_id": "INTEGER",
        "created_by": "TEXT",
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


def _scope_claims_dataset(conn):
    """Give csv_claims_data / csv_upload_meta an owner column.

    Existing rows were populated by fetches and manual uploads when the tool
    was single-user, and they are what every current reviewer sees - so they
    become the *shared* rows (owner NULL) rather than being attributed to
    whoever logs in first, or discarded.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='csv_claims_data'"
    ).fetchone()
    if not row or "owner_user_id" in (row[0] or ""):
        return

    conn.execute("""
        CREATE TABLE csv_claims_data_rebuilt (
          owner_user_id INTEGER NOT NULL DEFAULT 0,
          registration_id TEXT NOT NULL,
          row_json TEXT NOT NULL,
          PRIMARY KEY (owner_user_id, registration_id)
        )
    """)
    conn.execute(
        "INSERT INTO csv_claims_data_rebuilt (owner_user_id, registration_id, row_json) "
        "SELECT 0, registration_id, row_json FROM csv_claims_data")
    conn.execute("DROP TABLE csv_claims_data")
    conn.execute("ALTER TABLE csv_claims_data_rebuilt RENAME TO csv_claims_data")

    conn.execute("""
        CREATE TABLE csv_upload_meta_rebuilt (
          owner_user_id INTEGER NOT NULL PRIMARY KEY DEFAULT 0,
          filename TEXT NOT NULL,
          uploaded_at TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          columns_json TEXT NOT NULL
        )
    """)
    conn.execute(
        "INSERT INTO csv_upload_meta_rebuilt "
        "(owner_user_id, filename, uploaded_at, row_count, columns_json) "
        "SELECT 0, filename, uploaded_at, row_count, columns_json FROM csv_upload_meta")
    conn.execute("DROP TABLE csv_upload_meta")
    conn.execute("ALTER TABLE csv_upload_meta_rebuilt RENAME TO csv_upload_meta")


def _scope_rollup_cache(conn):
    """Add user_id to the rollup cache. It is a pure cache, so the old rows are
    dropped rather than migrated - recomputing costs one page load and no AWS
    spend, while guessing whose toggles produced them would be wrong."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='claim_rollup_cache'"
    ).fetchone()
    if not row or "user_id" in (row[0] or ""):
        return
    conn.execute("DROP TABLE claim_rollup_cache")
    conn.execute("""
        CREATE TABLE claim_rollup_cache (
          user_id INTEGER NOT NULL DEFAULT 0,
          claim_id TEXT NOT NULL,
          cache_key TEXT NOT NULL,
          rollup_json TEXT NOT NULL,
          computed_at TEXT NOT NULL,
          PRIMARY KEY (user_id, claim_id)
        )
    """)


def _migrate(conn):
    _drop_rules_type_check(conn)
    _scope_claims_dataset(conn)
    _scope_rollup_cache(conn)
    for table, columns in _MIGRATED_COLUMNS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for column, ddl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# Several people use this at once, so every connection needs the same two
# settings. WAL lets readers work while a writer holds the file (measured 4x
# faster with 10 concurrent writers), and the busy timeout makes a writer wait
# for its turn instead of raising "database is locked" - a fetch doing bulk
# inserts can hold the write lock for a while.
BUSY_TIMEOUT_MS = 15000


def _configure(conn):
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    # Durable enough for this workload and much cheaper per commit than FULL;
    # WAL already protects against application crashes, and the cost of losing
    # the last few writes to a machine crash is a reprocess, not lost claims.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def get_db():
    if "db" not in g:
        g.db = _configure(sqlite3.connect(
            current_app.config["DATABASE_PATH"],
            timeout=BUSY_TIMEOUT_MS / 1000))
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
        conn = _configure(sqlite3.connect(
            app.config["DATABASE_PATH"], timeout=BUSY_TIMEOUT_MS / 1000))
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        conn.close()
    app.teardown_appcontext(close_db)
