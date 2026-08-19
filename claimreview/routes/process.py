import threading

from flask import Blueprint, current_app, jsonify

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
    app = current_app._get_current_object()
    thread = threading.Thread(
        target=processing.process_claim, args=(app, claim_id, claim_path), daemon=True
    )
    thread.start()
    return jsonify({"status": "started", "total": len(docs)}), 202


@bp.route("/api/claims/<claim_id>/process/status")
def process_status(claim_id):
    state = progress.get(claim_id)
    if state is None:
        return jsonify({"status": "not_started"})
    return jsonify(state)
