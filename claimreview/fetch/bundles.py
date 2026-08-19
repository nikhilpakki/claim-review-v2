"""Reclaiming disk from downloaded claim bundles.

Deliberately free of psycopg/pandas imports so the delete action works even
where the pipeline itself cannot run (a machine without warehouse access can
still be the one that fills up).

What survives a deletion: the Textract cache is keyed by document content
hash, so OCR results, review notes and processing history are all untouched -
only the source files go, which means document previews for those claims stop
working and the claims disappear from the folder listing. They can be fetched
again from the same run parameters.
"""
import os
import shutil

from .. import fetch_progress, rollup_cache
from . import runs


class BundleDeleteError(Exception):
    pass


def _folder_size(path):
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total


def _safe_child(destination, claim_id):
    """The claim's folder under `destination`, or None if the id does not
    resolve to a direct child - registration ids come from the database, but a
    path built from stored data still gets checked before anything is deleted."""
    candidate = os.path.normpath(os.path.join(destination, claim_id))
    parent = os.path.normpath(destination)
    if os.path.dirname(candidate) != parent or candidate == parent:
        return None
    return candidate


def delete_run_bundles(run_id):
    """Delete the claim folders this run downloaded. Returns
    {deleted, missing, bytes_freed, destination}."""
    record = runs.get_run(run_id)
    if record is None:
        raise BundleDeleteError("Unknown run")

    active = fetch_progress.active_run_id()
    if active:
        raise BundleDeleteError(
            f"A fetch is running (run {active}); wait for it to finish before deleting files."
        )

    destination = record["destination"]
    if not os.path.isdir(destination):
        raise BundleDeleteError(f"Destination folder no longer exists: {destination}")

    claim_ids = runs.run_claim_ids(run_id)
    deleted = missing = bytes_freed = 0
    errors = []

    for claim_id in claim_ids:
        folder = _safe_child(destination, claim_id)
        if folder is None:
            errors.append(f"{claim_id}: refused (not a direct child of the destination)")
            continue
        if not os.path.isdir(folder):
            missing += 1
            continue
        size = _folder_size(folder)
        try:
            shutil.rmtree(folder)
        except OSError as exc:
            errors.append(f"{claim_id}: {exc}")
            continue
        deleted += 1
        bytes_freed += size
        rollup_cache.invalidate(claim_id)

    runs.mark_bundles_deleted(run_id)
    return {
        "deleted": deleted,
        "missing": missing,
        "bytes_freed": bytes_freed,
        "destination": destination,
        "errors": errors,
    }
