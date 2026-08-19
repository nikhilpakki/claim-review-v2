from flask import Blueprint, current_app, jsonify, request

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
    reviewer = (data.get("reviewer") or "").strip() or current_app.config.get("REVIEWER_NAME") or None

    db = get_db()
    db.execute(
        "INSERT INTO reviews (claim_id, document_path, status, notes, reviewer) VALUES (?, ?, ?, ?, ?)",
        (claim_id, document_path, status, notes, reviewer),
    )
    db.commit()
    return jsonify({"status": "ok"}), 201


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
