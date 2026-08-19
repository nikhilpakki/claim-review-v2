import threading

_lock = threading.Lock()
# claim_id -> dict(status, total, processed, cached, failed, current_files,
#                   total_pages, pages_done, error)
_runs = {}


def start(claim_id, total):
    with _lock:
        _runs[claim_id] = {
            "status": "running",
            "total": total,
            "processed": 0,
            "cached": 0,
            "failed": 0,
            "current_files": [],
            "total_pages": 0,
            "pages_done": 0,
            "error": None,
        }


def is_running(claim_id):
    with _lock:
        run = _runs.get(claim_id)
        return bool(run and run["status"] == "running")


def update(claim_id, **fields):
    with _lock:
        if claim_id in _runs:
            _runs[claim_id].update(fields)


def increment(claim_id, key, amount=1):
    """Adds `amount` to a numeric field (processed/cached/failed/
    total_pages/pages_done)."""
    with _lock:
        run = _runs.get(claim_id)
        if not run:
            return
        run[key] = run.get(key, 0) + amount


def add_in_flight(claim_id, file_name):
    """Marks a document as currently being analyzed. Several documents can
    be in flight at once now that pages are processed concurrently -
    current_files is a list, not a single "current file"."""
    with _lock:
        run = _runs.get(claim_id)
        if run and file_name not in run["current_files"]:
            run["current_files"].append(file_name)


def remove_in_flight(claim_id, file_name):
    with _lock:
        run = _runs.get(claim_id)
        if run and file_name in run["current_files"]:
            run["current_files"].remove(file_name)


def finish(claim_id, status="completed", error=None):
    with _lock:
        if claim_id in _runs:
            _runs[claim_id]["status"] = status
            _runs[claim_id]["current_files"] = []
            _runs[claim_id]["error"] = error


def get(claim_id):
    with _lock:
        run = _runs.get(claim_id)
        return dict(run) if run else None
