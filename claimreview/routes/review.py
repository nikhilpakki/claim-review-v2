from flask import Blueprint, jsonify, request

from .. import users
from ..db import get_db

bp = Blueprint("review", __name__)

VALID_STATUSES = {"approved", "flagged", "rejected"}


@bp.route("/api/claims/<claim_id>/review", methods=["POST"])
def submit_review(claim_id):
    data = request.get_json(silent=True) or request.form
    status = (data.get("status") or "").strip()
    if status not in VALID_STATUSES:
        return jsonify({"error": f"status must be one of {sorted(VALID_STATUSES)}"}), 400

    notes = (data.get("notes") or "").strip()
    document_path = (data.get("document_path") or "").strip() or None

    # Attribution comes from the session, not the request body. The reviewer
    # name used to be typed into the form, which is no basis for counting
    # anyone's progress - and hypothesis progress is counted from these rows.
    # `reviewer` is kept as a display-name snapshot so an old decision still
    # names its reviewer after the account is renamed or removed.
    user = users.current_user()
    reviewer_user_id = user["id"] if user else None
    reviewer = (user["display_name"] or user["username"]) if user else None

    db = get_db()
    db.execute(
        "INSERT INTO reviews (claim_id, document_path, status, notes, reviewer, reviewer_user_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (claim_id, document_path, status, notes, reviewer, reviewer_user_id),
    )
    db.commit()
    return jsonify({"status": "ok", "reviewer": reviewer}), 201


@bp.route("/api/claims/<claim_id>/reviews")
def list_reviews(claim_id):
    document_path = request.args.get("document_path")
    db = get_db()
    if document_path:
        rows = db.execute(
            "SELECT * FROM reviews WHERE claim_id=? AND document_path=? ORDER BY created_at DESC",
            (claim_id, document_path),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM reviews WHERE claim_id=? ORDER BY created_at DESC", (claim_id,)
        ).fetchall()

    reviews = [dict(row) for row in rows]
    current = reviews[0] if reviews else None
    return jsonify({"reviews": reviews, "current": current})
