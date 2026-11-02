from flask import Blueprint, jsonify, request

from .. import evidence, users
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
    cursor = db.execute(
        "INSERT INTO reviews (claim_id, document_path, status, notes, reviewer, reviewer_user_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (claim_id, document_path, status, notes, reviewer, reviewer_user_id),
    )
    db.commit()
    # Whatever this reviewer marked while reading the claim is copied onto the
    # decision and then cleared, so the record says what the decision was based
    # on and the next decision starts from an empty list rather than silently
    # re-attaching the last one's pages.
    attached = evidence.attach_to_review(cursor.lastrowid, reviewer_user_id, claim_id)
    return jsonify({"status": "ok", "reviewer": reviewer,
                    "evidence_attached": attached}), 201


@bp.route("/api/claims/<claim_id>/reviews")
def list_reviews(claim_id):
    document_path = request.args.get("document_path")
    db = get_db()
    # id DESC breaks the tie, as bookmarks.py already does. created_at is only
    # accurate to the second, and the first row of this list is what the panel
    # shows as the current status - so two decisions a moment apart could
    # otherwise report the older one as the claim's standing verdict.
    if document_path:
        rows = db.execute(
            "SELECT * FROM reviews WHERE claim_id=? AND document_path=? "
            "ORDER BY created_at DESC, id DESC",
            (claim_id, document_path),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT * FROM reviews WHERE claim_id=? ORDER BY created_at DESC, id DESC",
            (claim_id,)
        ).fetchall()

    reviews = [dict(row) for row in rows]
    attached = evidence.for_reviews([r["id"] for r in reviews])
    for review in reviews:
        review["evidence"] = attached.get(review["id"], [])
    current = reviews[0] if reviews else None
    return jsonify({"reviews": reviews, "current": current,
                    # The pending list belongs to whoever is reading the page,
                    # so it travels with the history rather than needing a
                    # second request the panel would have to sequence.
                    "pending_evidence": evidence.list_marks(
                        users.current_user_id(), claim_id)})


@bp.route("/api/claims/<claim_id>/evidence", methods=["GET", "POST"])
def claim_evidence(claim_id):
    """This reviewer's marked pages for this claim.

    POST does all three of mark, re-note and unmark, picked by `action`,
    because the preview button is one toggle and splitting it across verbs
    would only mean the front end deciding which to call.
    """
    user_id = users.current_user_id()
    if request.method == "GET":
        return jsonify({"marks": evidence.list_marks(user_id, claim_id)})

    data = request.get_json(silent=True) or request.form
    action = (data.get("action") or "mark").strip()
    document_path = (data.get("document_path") or "").strip()
    page_number = data.get("page_number")
    try:
        if action == "unmark":
            evidence.unmark(user_id, claim_id, document_path, page_number)
        elif action == "clear":
            evidence.clear(user_id, claim_id)
        else:
            evidence.mark(user_id, claim_id, document_path, page_number,
                          data.get("note"))
    except evidence.EvidenceError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"marks": evidence.list_marks(user_id, claim_id)})
