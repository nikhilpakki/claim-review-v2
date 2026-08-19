from flask import Blueprint, jsonify, request

from .. import root_state, search

bp = Blueprint("search_routes", __name__)


@bp.route("/api/claims/<claim_id>/search")
def api_search(claim_id):
    query = request.args.get("q", "")
    try:
        claim_path = root_state.get_claim_path(claim_id)
    except (ValueError, FileNotFoundError):
        return jsonify({"error": "Claim not found"}), 404

    documents, locations = search.load_claim_documents(claim_path)
    result = search.search_kvs(documents, query, locations=locations)
    return jsonify(result)
