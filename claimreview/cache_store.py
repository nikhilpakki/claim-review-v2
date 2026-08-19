import hashlib
import json
import os

from flask import current_app


_hash_cache = {}  # abs_path -> (mtime_ns, size, hash) - avoids re-reading/re-hashing
                  # unchanged files on every request (claim listing pages hash
                  # every file in every claim on every load)


def hash_file(file_path):
    """Content hash used as the cache key, so identical bytes are recognized
    as already processed even if the file was renamed or moved. Memoized by
    (mtime, size): a real content change always changes at least one of
    those, so the cache can't go stale under normal file edits."""
    stat = os.stat(file_path)
    cached = _hash_cache.get(file_path)
    if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]

    digest = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    file_hash = digest.hexdigest()
    _hash_cache[file_path] = (stat.st_mtime_ns, stat.st_size, file_hash)
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


def load_cached_result(file_hash):
    path = textract_cache_path(file_hash)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            # A corrupt cache file (e.g. an interrupted write) is treated as
            # a cache miss rather than a hard failure - reprocessing rebuilds
            # it cleanly instead of getting permanently stuck.
            return None


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
