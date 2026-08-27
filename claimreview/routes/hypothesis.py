"""The Hypotheses page.

Everyone can see the list and each hypothesis's progress; creating, editing,
deleting and processing need the manage-hypotheses capability (admins have it
implicitly, reviewers by grant). That split is the point: a hypothesis states
what the team is investigating, so it has to be visible to the people doing the
reviewing even though they do not set it.
"""
from flask import (Blueprint, current_app, jsonify, redirect, render_template,
                   request, url_for)

from .. import hypotheses, hypothesis_period, users

bp = Blueprint("hypothesis", __name__)


def _is_ajax():
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _context(**extra):
    user = users.current_user()
    return {
        "hypotheses": hypotheses.list_hypotheses(),
        "can_manage": hypotheses.can_manage(user),
        "period_choices": hypothesis_period.PERIOD_CHOICES,
        "default_period": hypothesis_period.DEFAULT_PERIOD,
        "confidence_levels": sorted(hypothesis_period.CONFIDENCE_LEVELS),
        "default_confidence": hypothesis_period.DEFAULT_CONFIDENCE,
        "default_margin": hypothesis_period.DEFAULT_MARGIN,
        "edit_hypothesis": None,
        "error": None,
        **extra,
    }


def _require_manage():
    """None if this user may manage hypotheses, else the response to return."""
    if hypotheses.can_manage(users.current_user()):
        return None
    message = ("Creating and editing hypotheses is limited to administrators and reviewers "
               "who have been granted it. You can see every hypothesis and its progress, and "
               "you can review the claims fetched under one.")
    if _is_ajax():
        return jsonify({"error": message}), 403
    return render_template("forbidden.html", heading="Not allowed",
                           message=message), 403


@bp.route("/hypotheses")
def view_hypotheses():
    return render_template("hypotheses.html", **_context())


@bp.route("/hypotheses/<int:hypothesis_id>/edit")
def edit_hypothesis_form(hypothesis_id):
    forbidden = _require_manage()
    if forbidden:
        return forbidden
    hypothesis = hypotheses.get_hypothesis(hypothesis_id)
    if hypothesis is None:
        return redirect(url_for("hypothesis.view_hypotheses"))
    return render_template("hypotheses.html", **_context(edit_hypothesis=hypothesis))


@bp.route("/hypotheses/create", methods=["POST"])
def create_hypothesis():
    forbidden = _require_manage()
    if forbidden:
        return forbidden
    try:
        hypotheses.create_hypothesis(request.form, users.current_user())
    except hypotheses.HypothesisError as exc:
        return render_template("hypotheses.html", **_context(error=str(exc))), 400
    return redirect(url_for("hypothesis.view_hypotheses"))


@bp.route("/hypotheses/<int:hypothesis_id>/update", methods=["POST"])
def update_hypothesis(hypothesis_id):
    forbidden = _require_manage()
    if forbidden:
        return forbidden
    try:
        hypotheses.update_hypothesis(hypothesis_id, request.form)
    except hypotheses.HypothesisError as exc:
        hypothesis = hypotheses.get_hypothesis(hypothesis_id)
        return render_template("hypotheses.html",
                               **_context(edit_hypothesis=hypothesis, error=str(exc))), 400
    return redirect(url_for("hypothesis.view_hypotheses"))


@bp.route("/hypotheses/<int:hypothesis_id>/delete", methods=["POST"])
def delete_hypothesis(hypothesis_id):
    forbidden = _require_manage()
    if forbidden:
        return forbidden
    hypotheses.delete_hypothesis(hypothesis_id)
    return redirect(url_for("hypothesis.view_hypotheses"))


@bp.route("/hypotheses/<int:hypothesis_id>/process", methods=["POST"])
def process_hypothesis(hypothesis_id):
    """Count the population and freeze the KPIs.

    Synchronous on purpose: measured against the live warehouse this is 0.6s
    for a single day and 2.5s for three months across all three tables. A
    background job would cost more in machinery than it saves in waiting.
    """
    forbidden = _require_manage()
    if forbidden:
        return forbidden
    try:
        hypothesis = hypotheses.process_hypothesis(hypothesis_id)
    except hypotheses.HypothesisError as exc:
        if _is_ajax():
            return jsonify({"error": str(exc)}), 400
        return render_template("hypotheses.html", **_context(error=str(exc))), 400
    except Exception as exc:  # a warehouse that is down should not 500 the page
        current_app.logger.exception("Hypothesis processing failed")
        message = f"Could not reach the warehouse to count claims: {exc}"
        if _is_ajax():
            return jsonify({"error": message}), 502
        return render_template("hypotheses.html", **_context(error=message)), 502

    if _is_ajax():
        return jsonify({
            "status": "ok",
            "claim_count": hypothesis["claim_count"],
            "sample_size": hypothesis["sample_size"],
            "processed_at": hypothesis["processed_at"],
            "window_from": hypothesis["processed_window_from"],
            "window_to": hypothesis["processed_window_to"],
            "period_description": hypothesis["period_description"],
            "reviews_remaining": hypothesis["reviews_remaining"],
        })
    return redirect(url_for("hypothesis.view_hypotheses"))
