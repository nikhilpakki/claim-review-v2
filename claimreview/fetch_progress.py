"""Live state for the running claim fetch, mirroring progress.py.

progress.py tracks per-claim Textract processing, of which many can run at
once; a fetch is different - it is a single batch job that owns the S3 and
warehouse connections, so only one runs at a time and the registry is keyed by
run id with an `active` pointer.

In-memory only, and deliberately so: the durable record of a run lives in
SQLite (fetch/runs.py). This is just what the /fetch page polls while the run
is in flight.
"""
import threading
from datetime import datetime, timezone

_lock = threading.Lock()
_runs = {}          # run_id -> state dict
_cancels = {}       # run_id -> threading.Event
_active_run_id = None

# Cap on retained log lines per run: a no-limit fetch of every eligible claim
# would otherwise grow this without bound.
LOG_LIMIT = 300
RECENT_LIMIT = 25


def _now():
    return datetime.now(timezone.utc).isoformat()


def start(run_id, destination, params):
    """Register a run as starting. Returns its cancel Event, which the pipeline
    checks between claims."""
    global _active_run_id
    cancel = threading.Event()
    with _lock:
        _runs[run_id] = {
            "run_id": run_id,
            "status": "running",
            "phase": "selecting",
            "destination": destination,
            "params": params,
            "source": None,
            "total": 0,
            "done": 0,
            "ok": 0,
            "failed": 0,
            "claim_ids": [],
            "recent": [],
            "log": [],
            "error": None,
            "started_at": _now(),
            "finished_at": None,
            "result": None,
        }
        _cancels[run_id] = cancel
        _active_run_id = run_id
    return cancel


def is_running(run_id=None):
    with _lock:
        if run_id is None:
            run = _runs.get(_active_run_id) if _active_run_id else None
        else:
            run = _runs.get(run_id)
        return bool(run and run["status"] == "running")


def active_run_id():
    with _lock:
        run = _runs.get(_active_run_id) if _active_run_id else None
        return _active_run_id if run and run["status"] == "running" else None


def update(run_id, **fields):
    with _lock:
        if run_id in _runs:
            _runs[run_id].update(fields)


def set_selected(run_id, claim_ids, source):
    with _lock:
        run = _runs.get(run_id)
        if run:
            run["claim_ids"] = list(claim_ids)
            run["total"] = len(claim_ids)
            run["source"] = source


def claim_done(run_id, registration_id, download_status, extraction_status, ok):
    with _lock:
        run = _runs.get(run_id)
        if not run:
            return
        run["done"] += 1
        run["ok" if ok else "failed"] += 1
        run["recent"].insert(0, {
            "registration_id": registration_id,
            "download_status": download_status,
            "extraction_status": extraction_status,
            "ok": ok,
        })
        del run["recent"][RECENT_LIMIT:]


def log(run_id, message, level="info"):
    with _lock:
        run = _runs.get(run_id)
        if not run:
            return
        run["log"].append({"at": _now(), "message": message, "level": level})
        if len(run["log"]) > LOG_LIMIT:
            del run["log"][:-LOG_LIMIT]


def cancel(run_id):
    """Ask a run to stop. Claims already in flight finish (killing a download
    mid-write would leave a half-written bundle); queued claims are skipped."""
    with _lock:
        event = _cancels.get(run_id)
        run = _runs.get(run_id)
        if run and run["status"] == "running":
            run["phase"] = "cancelling"
    if event:
        event.set()
        return True
    return False


def is_cancelled(run_id):
    with _lock:
        event = _cancels.get(run_id)
    return bool(event and event.is_set())


def finish(run_id, status="completed", error=None, result=None):
    global _active_run_id
    with _lock:
        run = _runs.get(run_id)
        if run:
            run["status"] = status
            run["phase"] = "done"
            run["error"] = error
            run["result"] = result
            run["finished_at"] = _now()
        _cancels.pop(run_id, None)
        if _active_run_id == run_id:
            _active_run_id = None


def get(run_id):
    with _lock:
        run = _runs.get(run_id)
        return dict(run) if run else None
