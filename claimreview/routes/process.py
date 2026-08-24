import threading

from flask import Blueprint, current_app, jsonify, request

from .. import claim_scanner, processing, progress, root_state

bp = Blueprint("process", __name__)


@bp.route("/api/claims/<claim_id>/process", methods=["POST"])
def start_process(claim_id):
    try:
        claim_path = root_state.get_claim_path(claim_id)
    except (ValueError, FileNotFoundError):
        return jsonify({"error": "Claim not found"}), 404

    if progress.is_running(claim_id):
        return jsonify({"error": "Already processing"}), 409

    docs = claim_scanner.scan_claim(claim_path)
    # ?force=stale re-analyzes documents whose cached result predates the
    # current Textract profile. It is opt-in because re-analysis is a real
    # per-page AWS charge, never something a page load should trigger.
    force_stale = request.args.get("force") == "stale"
    app = current_app._get_current_object()
    thread = threading.Thread(
        target=processing.process_claim, args=(app, claim_id, claim_path),
        kwargs={"force_stale": force_stale}, daemon=True,
    )
    thread.start()
    return jsonify({"status": "started", "total": len(docs), "force_stale": force_stale}), 202


@bp.route("/api/claims/<claim_id>/process/status")
def process_status(claim_id):
    state = progress.get(claim_id)
    if state is None:
        return jsonify({"status": "not_started"})
    return jsonify(state)
