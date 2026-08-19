import os

from flask import current_app

from . import cache_store


def list_claims(root_dir):
    """Immediate subfolders of root_dir - each is a claim bundle (registration ID)."""
    entries = []
    with os.scandir(root_dir) as it:
        for entry in it:
            if entry.is_dir():
                entries.append({"claim_id": entry.name, "path": entry.path})
    entries.sort(key=lambda e: e["claim_id"])
    return entries


def scan_claim(claim_path):
    """Recursively find every .pdf/.jpg/.jpeg under claim_path, ignoring
    everything else (in particular the many .json metadata files).

    Returns a list of {doc_id, rel_path, abs_path, ext, size, group} where
    doc_id is the URL-safe relative path (used to identify the doc in
    routes/cache lookups) and group is 'payer'/'provider'/other top-level
    folder name, for display grouping.
    """
    supported = current_app.config["SUPPORTED_EXTENSIONS"]
    docs = []
    for dirpath, _dirs, files in os.walk(claim_path):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in supported:
                continue
            abs_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(abs_path, claim_path)
            rel_posix = rel_path.replace(os.sep, "/")
            top = rel_posix.split("/")[0] if "/" in rel_posix else ""
            dir_path, _, file_name = rel_posix.rpartition("/")
            docs.append({
                "doc_id": rel_posix,
                "rel_path": rel_posix,
                "file_name": file_name or rel_posix,
                "dir_path": dir_path,
                "abs_path": abs_path,
                "ext": ext,
                "size": os.path.getsize(abs_path),
                "group": top or "(root)",
            })
    docs.sort(key=lambda d: d["rel_path"].lower())
    return docs


def scan_claim_hashed(claim_path):
    """scan_claim() plus each doc's content hash, without touching the
    Textract cache. The hashes alone identify the claim's content, which is
    what the claims list needs to decide whether its cached rollup is still
    valid - reading every document's cached analysis just to find out it was
    not needed is the expensive half."""
    docs = scan_claim(claim_path)
    for doc in docs:
        doc["file_hash"] = cache_store.hash_file(doc["abs_path"])
    return docs


def attach_cached_results(docs):
    """Load each already-hashed doc's Textract result (None if not yet
    processed). Split out from the scan so callers can skip it on a cache hit."""
    for doc in docs:
        doc["cached_result"] = cache_store.load_cached_result(doc["file_hash"])
    return docs


def scan_claim_cached(claim_path):
    """scan_claim() plus each doc's content hash and (if present) its
    already-Textract-processed cached result, computed once per file.
    Callers that need both a quality/rule rollup and rule evaluation for the
    same claim should scan once via this and reuse it, rather than each
    independently re-hashing (SHA-256 over the whole file) and re-reading
    the cache - the previous per-caller rescans were the home page's main
    slow path once a root has any real number of claims/documents."""
    return attach_cached_results(scan_claim_hashed(claim_path))


def resolve_doc(claim_path, doc_id):
    """Map a doc_id (relative path) back to its absolute path, guarding
    against path traversal outside claim_path."""
    abs_path = os.path.normpath(os.path.join(claim_path, doc_id))
    claim_real = os.path.normpath(claim_path)
    if not (abs_path == claim_real or abs_path.startswith(claim_real + os.sep)):
        raise ValueError("Invalid document id")
    if not os.path.isfile(abs_path):
        raise FileNotFoundError(doc_id)
    return abs_path
