import hashlib
import json
import os

from flask import current_app, g, has_app_context


_hash_cache = {}  # abs_path -> (mtime_ns, size, hash) - avoids re-reading/re-hashing
                  # unchanged files on every request (claim listing pages hash
                  # every file in every claim on every load)

# Two layers, for two different costs. The dict above answers repeat loads
# inside one process; the doc_hash_cache table answers the first load after a
# restart, which otherwise re-reads every file in the folder - measured at ~50
# seconds for 3,000 claims, almost all of it in open() rather than in SHA-256.
_DB_CACHE_KEY = "_doc_hash_rows"
_DB_PENDING_KEY = "_doc_hash_pending"


def _db_rows():
    """{path: (size, mtime_ns, hash)} for this app context, loaded once.

    One read of the whole table beats a query per file: the claims list asks
    about thousands of files in a single request, and the table is small enough
    (one row per document ever seen) that loading it is a few milliseconds.
    """
    if not has_app_context():
        return None
    if _DB_CACHE_KEY not in g:
        from .db import get_db
        try:
            rows = get_db().execute(
                "SELECT path, size, mtime_ns, file_hash FROM doc_hash_cache").fetchall()
        except Exception:  # a database that predates the table, or is unreachable
            setattr(g, _DB_CACHE_KEY, {})
            return getattr(g, _DB_CACHE_KEY)
        setattr(g, _DB_CACHE_KEY, {
            row["path"]: (row["size"], row["mtime_ns"], row["file_hash"]) for row in rows})
    return getattr(g, _DB_CACHE_KEY)


def _remember(file_path, stat, file_hash):
    rows = _db_rows()
    if rows is None:
        return
    rows[file_path] = (stat.st_size, stat.st_mtime_ns, file_hash)
    pending = g.setdefault(_DB_PENDING_KEY, {})
    pending[file_path] = (stat.st_size, stat.st_mtime_ns, file_hash)


def flush_hash_cache(_exc=None):
    """Write the request's newly discovered hashes. Registered as a teardown.

    Never allowed to break the response it is attached to: a failure here costs
    a slower next load, nothing more.
    """
    if not has_app_context():
        return
    pending = g.pop(_DB_PENDING_KEY, None)
    if not pending:
        return
    try:
        from .db import get_db
        db = get_db()
        db.executemany(
            "INSERT INTO doc_hash_cache (path, size, mtime_ns, file_hash) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, "
            "file_hash=excluded.file_hash",
            [(path, size, mtime, h) for path, (size, mtime, h) in pending.items()])
        db.commit()
    except Exception:
        if has_app_context():
            current_app.logger.debug("could not persist document hashes", exc_info=True)


def hash_file(file_path):
    """Content hash used as the cache key, so identical bytes are recognized
    as already processed even if the file was renamed or moved.

    Memoized by (mtime, size), in memory and in the database: a real content
    change always changes at least one of those, so the cache cannot go stale
    under normal file edits, and claim bundles are written once by a fetch
    rather than edited in place.
    """
    stat = os.stat(file_path)
    cached = _hash_cache.get(file_path)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]

    rows = _db_rows()
    stored = rows.get(file_path) if rows else None
    if stored and stored[0] == stat.st_size and stored[1] == stat.st_mtime_ns:
        _hash_cache[file_path] = (stat.st_mtime_ns, stat.st_size, stored[2])
        return stored[2]

    digest = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    file_hash = digest.hexdigest()
    _hash_cache[file_path] = (stat.st_mtime_ns, stat.st_size, file_hash)
    _remember(file_path, stat, file_hash)
    return file_hash


def textract_cache_path(file_hash):
    return os.path.join(current_app.config["TEXTRACT_CACHE_DIR"], f"{file_hash}.json")


def pages_cache_dir(file_hash):
    return os.path.join(current_app.config["PAGES_CACHE_DIR"], file_hash)


def page_image_path(image_rel):
    """Resolve a page's stored "<hash>/page-N.jpg" reference to an absolute
    path on disk, e.g. for re-reading an already-rendered image without
    re-running Textract."""
    return os.path.join(current_app.config["PAGES_CACHE_DIR"], image_rel)


def has_cached_result(file_hash):
    return os.path.exists(textract_cache_path(file_hash))


def load_cached_result_at(path):
    """A document's cached Textract result, or None.

    Takes a resolved path rather than a hash so it can be called without an
    application context - which is also what made it measurable against a
    thread pool (see claim_scanner.attach_cached_results).
    """
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            # A corrupt cache file (e.g. an interrupted write) is treated as a
            # cache miss rather than a hard failure - reprocessing rebuilds it
            # cleanly instead of getting permanently stuck.
            return None


def load_cached_result(file_hash):
    return load_cached_result_at(textract_cache_path(file_hash))


def save_cached_result(file_hash, data):
    """Writes via a temp file + atomic rename, so a serialization error (or
    a crash mid-write) can never leave a half-written, corrupt cache file
    behind - the previous valid file (if any) stays intact until the new
    one is fully written."""
    path = textract_cache_path(file_hash)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)
