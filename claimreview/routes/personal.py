"""Bookmarks, and the review board.

Bookmarks are strictly personal - every query is scoped to the signed-in user,
and there is no way to ask for somebody else's.

Reviews are not. A decision is a statement about a claim rather than a private
note, the team cross-checks each other's work, and the validation summary that
leaves the tool is assembled out of several people's decisions at once - so
the board shows everybody's, filtered by whoever is wanted. Changing one is
another matter: only an administrator can edit or delete a decision, and every
such change writes an audit row (see review_list.py).
"""
from urllib.parse import parse_qsl, urlencode

from flask import (Blueprint, Response, jsonify, redirect, render_template,
                   request, url_for)

from .. import bookmarks, case_doc, evidence, my_reviews, review_list, users
from .auth import admin_required

bp = Blueprint("personal", __name__)

# The filters the board carries in its URL, so a redirect after an edit comes
# back to the list the administrator was looking at rather than to all of it.
LIST_PARAMS = ("status", "reviewer", "claim", "hypothesis", "sort", "superseded")

# A selection big enough to be a mistake. Every case re-reads the claim's
# documents to render its evidence pages, so an accidental "select all" over a
# whole folder would be a very long request rather than a quick one.
MAX_EXPORT_CASES = 100


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


# -------------------------------------------------------------- review board


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _filters():
    """The board's filters, read off the query string."""
    sort = request.args.get("sort") or review_list.DEFAULT_SORT
    return {
        "status": (request.args.get("status") or "").strip() or None,
        "reviewer": _int_or_none(request.args.get("reviewer")),
        "claim": (request.args.get("claim") or "").strip(),
        "hypothesis": _int_or_none(request.args.get("hypothesis")),
        "sort": sort if sort in review_list.SORTS else review_list.DEFAULT_SORT,
        "superseded": request.args.get("superseded") == "1",
    }


def _list_query(params):
    pairs = [("status", params["status"] or ""),
             ("reviewer", "" if params["reviewer"] is None else params["reviewer"]),
             ("claim", params["claim"]),
             ("hypothesis", "" if params["hypothesis"] is None else params["hypothesis"]),
             ("sort", params["sort"]),
             ("superseded", "1" if params["superseded"] else "")]
    return urlencode([(key, value) for key, value in pairs if value not in ("", None)])


def _back_query(raw):
    """A returned-to list, rebuilt from only the keys this page has.

    The value arrives in a form field, so it is input: anything that is not one
    of this page's own filters is dropped, which leaves it unable to be
    anything but a query string on this route.
    """
    if not raw:
        return ""
    return urlencode([(key, value) for key, value
                      in parse_qsl(raw, keep_blank_values=False)
                      if key in LIST_PARAMS])


def _board_url(back):
    query = _back_query(back)
    return url_for("personal.view_reviews") + (("?" + query) if query else "")


@bp.route("/reviews")
def view_reviews():
    params = _filters()
    rows, truncated = review_list.search(
        status=params["status"], reviewer_user_id=params["reviewer"],
        claim_query=params["claim"], hypothesis_id=params["hypothesis"],
        sort=params["sort"], include_superseded=params["superseded"])

    # Whether each claim can still be opened: a decision outlives the bundle it
    # was made against, so a row whose files are gone is shown without a link
    # rather than one that bounces straight back to the claims list.
    available = {}
    for row in rows:
        claim_id = row["claim_id"]
        if claim_id not in available:
            available[claim_id] = bookmarks.claim_exists(claim_id)
        row["available"] = available[claim_id]

    edits = review_list.last_edits([row["id"] for row in rows])
    for row in rows:
        row["last_edit"] = edits.get(row["id"])

    user = users.current_user()
    return render_template(
        "reviews.html", rows=rows, truncated=truncated, params=params,
        list_query=_list_query(params), counts=review_list.counts(rows),
        reviewers=review_list.reviewers(),
        hypotheses=review_list.hypothesis_options(),
        statuses=review_list.STATUSES,
        unattributed=review_list.UNATTRIBUTED,
        max_export=MAX_EXPORT_CASES,
        deletions=review_list.deletions() if user and user["is_admin"] else [],
        my_totals=my_reviews.totals_for(users.current_user_id()))


@bp.route("/reviews/<int:review_id>/edit", methods=["POST"])
@admin_required
def edit_review(review_id):
    try:
        review_list.update(review_id, request.form.get("status"),
                           request.form.get("notes"), users.current_user())
    except review_list.ReviewError as exc:
        return render_template("forbidden.html", heading="Cannot change that",
                               message=str(exc),
                               back_url=_board_url(request.form.get("back")),
                               back_label="Back to reviews"), 400
    return redirect(_board_url(request.form.get("back")))


@bp.route("/reviews/<int:review_id>/delete", methods=["POST"])
@admin_required
def delete_review(review_id):
    try:
        review_list.delete(review_id, users.current_user())
    except review_list.ReviewError as exc:
        return render_template("forbidden.html", heading="Cannot delete that",
                               message=str(exc),
                               back_url=_board_url(request.form.get("back")),
                               back_label="Back to reviews"), 400
    return redirect(_board_url(request.form.get("back")))


@bp.route("/reviews/export", methods=["POST"])
def export_reviews():
    """The selected decisions as a Case Validation summary.

    Built and returned whole rather than streamed: a Word document is not
    readable in parts, and the evidence images have to be counted against the
    size caps before any of it is sent.
    """
    wanted = request.form.getlist("review_ids")
    if (request.form.get("format") or "doc") != "doc":
        return render_template("forbidden.html", heading="Not available yet",
                               message="The interactive HTML export is still "
                                       "being built. Choose the Word document "
                                       "for now.",
                               back_url=_board_url(request.form.get("back")),
                               back_label="Back to reviews"), 400
    if not wanted:
        return redirect(_board_url(request.form.get("back")))
    if len(wanted) > MAX_EXPORT_CASES:
        return render_template(
            "forbidden.html", heading="That is a lot of cases",
            message="Export up to {} cases at a time - {} were selected. "
                    "Narrow the filters and export in batches.".format(
                        MAX_EXPORT_CASES, len(wanted)),
            back_url=_board_url(request.form.get("back")),
            back_label="Back to reviews"), 400

    reviews = review_list.get_many(wanted)
    if not reviews:
        return redirect(_board_url(request.form.get("back")))

    attached = evidence.for_reviews([review["id"] for review in reviews])
    filename, payload, _report = case_doc.build(
        reviews, attached, user_id=users.current_user_id(),
        title=(request.form.get("title") or "").strip() or None)
    return Response(payload, mimetype=case_doc.MIME_TYPE, headers={
        "Content-Disposition": 'attachment; filename="{}"'.format(filename),
        "Content-Length": str(len(payload)),
    })
