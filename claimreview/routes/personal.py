"""The two pages that belong to one reviewer: Bookmarks and Reviews.

Both are strictly personal - every query is scoped to the signed-in user, and
there is no way to ask for somebody else's. They live in one blueprint because
they answer the same question from two sides: what have I set aside, and what
have I decided.
"""
from flask import (Blueprint, jsonify, redirect, render_template, request,
                   url_for)

from .. import bookmarks, my_reviews, users

bp = Blueprint("personal", __name__)


def _is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


# ------------------------------------------------------------------ bookmarks

@bp.route("/bookmarks")
def view_bookmarks():
    user_id = users.current_user_id()
    groups = bookmarks.grouped(user_id)
    # Whether each claim can still be opened: a bookmark outlives the bundle it
    # was made against, so an entry whose files are gone gets shown without a
    # link rather than one that 404s.
    for group in groups:
        for claim in group["claims"]:
            claim["available"] = bookmarks.claim_exists(claim["claim_id"])
    return render_template("bookmarks.html", groups=groups,
                           total=sum(len(g["claims"]) for g in groups))


@bp.route("/api/claims/<claim_id>/bookmarks")
def api_claim_bookmarks(claim_id):
    """What this claim is filed under, and everything it could be filed under."""
    user_id = users.current_user_id()
    return jsonify({
        "claim_id": claim_id,
        "labels": bookmarks.labels_for(user_id),
        "applied": bookmarks.labels_for_claim(user_id, claim_id),
        "defaults": bookmarks.DEFAULT_LABELS,
    })


@bp.route("/api/claims/<claim_id>/bookmark", methods=["POST"])
def api_add_bookmark(claim_id):
    data = request.get_json(silent=True) or request.form
    # A typed name wins over the dropdown: the reviewer only fills it in when
    # they mean to make a new one.
    label = (data.get("new_label") or "").strip() or (data.get("label") or "")
    user_id = users.current_user_id()
    try:
        stored = bookmarks.add(user_id, claim_id, label, data.get("note"))
    except bookmarks.BookmarkError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"status": "ok", "label": stored,
                    "labels": bookmarks.labels_for(user_id),
                    "applied": bookmarks.labels_for_claim(user_id, claim_id)}), 201


@bp.route("/api/claims/<claim_id>/bookmark/remove", methods=["POST"])
def api_remove_bookmark(claim_id):
    data = request.get_json(silent=True) or request.form
    user_id = users.current_user_id()
    bookmarks.remove(user_id, claim_id, (data.get("label") or "").strip())
    return jsonify({"status": "ok",
                    "labels": bookmarks.labels_for(user_id),
                    "applied": bookmarks.labels_for_claim(user_id, claim_id)})


@bp.route("/bookmarks/remove", methods=["POST"])
def remove_bookmark_form():
    """The Bookmarks page's own remove button, for a full-page POST."""
    bookmarks.remove(users.current_user_id(), request.form.get("claim_id"),
                     (request.form.get("label") or "").strip())
    return redirect(url_for("personal.view_bookmarks"))


# -------------------------------------------------------------------- reviews

@bp.route("/reviews")
def view_reviews():
    user_id = users.current_user_id()
    groups = my_reviews.grouped_for(user_id)
    for group in groups:
        for claim in group["claims"]:
            claim["available"] = bookmarks.claim_exists(claim["claim_id"])
    return render_template("reviews.html", groups=groups,
                           totals=my_reviews.totals_for(user_id),
                           no_hypothesis_label=my_reviews.NO_HYPOTHESIS)
