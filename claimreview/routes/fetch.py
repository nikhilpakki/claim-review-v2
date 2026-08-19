"""The Fetch page: pull claim bundles from client S3 into a review folder.

This is the review-app front end for the claim-bundle pipeline that used to be
run as `python daily_claim_bundle_pipeline.py ...` on the box. The form fields
map one-to-one onto that CLI's options, and both paths run the same
pipeline.run_pipeline().
"""
import os

from flask import (
    Blueprint, current_app, jsonify, render_template, request, url_for,
)

from .. import fetch_progress, root_state
from ..fetch import bundles, queries, runs as fetch_runs

# The pipeline pulls in psycopg/pandas/openpyxl. If they are not installed yet,
# the rest of the app (reviewing already-downloaded claims) must still work -
# so the import is guarded and the page explains what to install instead of
# taking the whole app down at startup.
try:
    from ..fetch import service
    FETCH_UNAVAILABLE = None
except Exception as exc:  # noqa: BLE001
    service = None
    FETCH_UNAVAILABLE = f"{type(exc).__name__}: {exc}"

bp = Blueprint("fetch", __name__)

# Claim ids shown in a preview response; the count is always exact.
PREVIEW_SAMPLE = 500


def _service_or_error():
    if service is None:
        return None, (
            jsonify({
                "error": "Claim fetching needs psycopg[binary], pandas and openpyxl. "
                         "Install them with: pip install -r requirements.txt",
                "detail": FETCH_UNAVAILABLE,
            }),
            503,
        )
    return service, None


def _options_or_error(run_id=None):
    """Build FetchOptions from the submitted form, or an error response."""
    svc, error = _service_or_error()
    if error:
        return None, error
    try:
        options = svc.build_options(
            request.form, request.files, current_app.config, run_id=run_id
        )
    except svc.FetchInputError as exc:
        return None, (jsonify({"error": str(exc)}), 400)
    return options, None


def _default_destination(config, recent_runs):
    """Where the form's destination box starts. The last run's folder, if it
    is still there - fetching twice in a row into two different places is
    almost never what someone means, and re-typing a chosen path each time is
    just friction. Falls back to the configured root."""
    for run in recent_runs:
        if run["destination"] and os.path.isdir(run["destination"]):
            return run["destination"]
    return config["BUNDLES_ROOT"]


@bp.route("/fetch")
def fetch_page():
    config = current_app.config
    active_id = fetch_progress.active_run_id()
    recent_runs = fetch_runs.list_runs(limit=10)
    return render_template(
        "fetch.html",
        unavailable=FETCH_UNAVAILABLE,
        default_destination=_default_destination(config, recent_runs),
        default_limit=config["FETCH_DEFAULT_LIMIT"],
        load_redshift_default=config["FETCH_LOAD_REDSHIFT_DEFAULT"],
        source_table=config["CLAIM_SOURCE_TABLE"],
        hospital_types=sorted(queries.HOSPITAL_TYPES.items()),
        default_exclude_codes=queries.DEFAULT_EXCLUDE_PROCEDURE_CODES,
        warehouse_configured=bool(config.get("REDSHIFT_HOST")),
        active_run_id=active_id,
        recent_runs=recent_runs,
    )


@bp.route("/api/fetch/preview", methods=["POST"])
def preview():
    """Run the selection query only - nothing is downloaded, nothing is
    written. Lets a mis-typed filter or an unintentionally huge selection be
    caught before any S3 traffic happens."""
    options, error = _options_or_error()
    if error:
        return error
    try:
        result = service.preview(options)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 502

    claim_ids = result["claim_ids"]
    return jsonify({
        "count": result["count"],
        "claim_ids": claim_ids[:PREVIEW_SAMPLE],
        "truncated": len(claim_ids) > PREVIEW_SAMPLE,
        "source": result["source"],
        # How the filter expressions were read - the comma/pipe distinction is
        # easy to get wrong, so the preview states it rather than implying it.
        "filters": result["filters"],
        "without_preauth_path": result["without_preauth_path"][:50],
        "without_preauth_count": len(result["without_preauth_path"]),
    })


@bp.route("/api/fetch/start", methods=["POST"])
def start():
    options, error = _options_or_error()
    if error:
        return error

    app = current_app._get_current_object()
    try:
        run_id = service.start(app, options)
    except service.FetchInputError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    return jsonify({"status": "started", "run_id": run_id}), 202


@bp.route("/api/fetch/<run_id>/status")
def status(run_id):
    state = fetch_progress.get(run_id)
    if state is not None:
        return jsonify(state)

    # Not in memory: either the app restarted, or this is an older run.
    record = fetch_runs.get_run(run_id)
    if record is None:
        return jsonify({"status": "not_found"}), 404
    claims = fetch_runs.run_claims(run_id)
    return jsonify({
        "run_id": run_id,
        "status": record["status"],
        "phase": "done",
        "destination": record["destination"],
        "source": record["source"],
        "total": record["claims_total"],
        "done": len(claims),
        "ok": record["claims_ok"],
        "failed": record["claims_failed"],
        "claim_ids": [c["registration_id"] for c in claims],
        "recent": [],
        "log": [],
        "error": record["error"],
        "started_at": record["started_at"],
        "finished_at": record["finished_at"],
        "result": {
            "run_id": run_id,
            "destination": record["destination"],
            "claims_total": record["claims_total"],
            "claims_ok": record["claims_ok"],
            "claims_failed": record["claims_failed"],
            "json_report": record["json_report"],
            "xlsx_report": record["xlsx_report"],
        },
    })


@bp.route("/api/fetch/<run_id>/cancel", methods=["POST"])
def cancel(run_id):
    """Stop after the claims currently downloading finish. Claims already
    downloaded are still extracted and loaded - a cancel should not throw away
    work that is already on disk."""
    if not fetch_progress.cancel(run_id):
        return jsonify({"error": "That run is not active"}), 404
    return jsonify({"status": "cancelling", "run_id": run_id})


@bp.route("/api/fetch/<run_id>/delete-files", methods=["POST"])
def delete_files(run_id):
    """Delete this run's downloaded bundles to reclaim disk. Review history and
    OCR results survive (the Textract cache is content-hashed); the claims
    themselves disappear from the folder until fetched again."""
    try:
        result = bundles.delete_run_bundles(run_id)
    except bundles.BundleDeleteError as exc:
        return jsonify({"error": str(exc)}), 409
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500
    return jsonify(result)


@bp.route("/api/fetch/<run_id>/adopt", methods=["POST"])
def adopt(run_id):
    """Point the review side at this run's destination folder and hand back the
    claims list URL, with the run's claims pre-selected."""
    record = fetch_runs.get_run(run_id)
    if record is None:
        return jsonify({"error": "Unknown run"}), 404

    destination = record["destination"]
    if not os.path.isdir(destination):
        return jsonify({"error": f"Destination folder no longer exists: {destination}"}), 400

    root_state.set_active_root(os.path.normpath(destination))
    return jsonify({
        "status": "ok",
        "root": destination,
        "next": url_for("claims.list_claims_view", fetch_run=run_id),
    })
