"""Sign-in, sign-out, and the admin-only account screen."""
from functools import wraps
from urllib.parse import quote

from flask import (Blueprint, current_app, redirect, render_template, request,
                   session, url_for)

from .. import users

bp = Blueprint("auth", __name__)

# Endpoints reachable without signing in. Everything else requires a session -
# this is a claims tool holding patient data, so the default is closed.
PUBLIC_ENDPOINTS = {"auth.login", "static"}


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if users.current_user() is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Rules and detection settings are administered centrally: they decide
    what everyone sees and what gets billed to AWS."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = users.current_user()
        if user is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if not user["is_admin"]:
            return render_template("forbidden.html"), 403
        return view(*args, **kwargs)
    return wrapped


def _safe_next(value):
    """Only same-site paths; an absolute or protocol-relative URL here would be
    an open redirect straight off the login form."""
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return None


@bp.route("/login", methods=["GET", "POST"])
def login():
    next_url = _safe_next(request.args.get("next") or request.form.get("next"))
    if users.current_user() is not None:
        return redirect(next_url or url_for("claims.list_claims_view"))

    error = None
    if request.method == "POST":
        user = users.authenticate(request.form.get("username"),
                                  request.form.get("password"))
        if user is None:
            error = "Wrong username or password."
        else:
            users.log_in(user)
            return redirect(next_url or url_for("claims.list_claims_view"))

    return render_template("login.html", error=error, next=next_url or "",
                           any_users=users.count_users() > 0), (401 if error else 200)


@bp.route("/logout", methods=["POST", "GET"])
def logout():
    users.log_out()
    return redirect(url_for("auth.login"))


# What each action says once it has succeeded. Looked up here rather than
# carried in the URL: the message survives the redirect without letting
# anything in the query string decide what the page tells an administrator.
SAVED_MESSAGES = {
    "create": "Account created.",
    "password": "Password changed.",
    "toggle_admin": "Administrator rights updated.",
    "toggle_hypotheses": "Hypothesis permissions updated.",
    "toggle_active": "Account updated.",
}


@bp.route("/admin/users")
@admin_required
def list_users():
    return render_template("users.html", users=users.list_users(),
                           current=users.current_user(), error=None,
                           message=SAVED_MESSAGES.get(request.args.get("done")))


@bp.route("/admin/users", methods=["POST"])
@admin_required
def save_user():
    action = request.form.get("action")
    error = message = None
    try:
        if action == "create":
            users.create_user(
                request.form.get("username"), request.form.get("password"),
                display_name=request.form.get("display_name"),
                is_admin=request.form.get("is_admin") == "on")
            message = "Account created."
        elif action == "password":
            users.set_password(int(request.form["user_id"]), request.form.get("password"))
            message = "Password changed."
        elif action == "toggle_admin":
            user_id = int(request.form["user_id"])
            target = users.get_user(user_id)
            # Removing the last administrator would leave nobody able to
            # manage rules, settings or accounts.
            if target and target["is_admin"] and _admin_count() <= 1:
                error = "That is the only administrator - promote someone else first."
            else:
                users.set_admin(user_id, not (target and target["is_admin"]))
                message = "Administrator rights updated."
        elif action == "toggle_hypotheses":
            # Admins already manage hypotheses by virtue of being admins, so
            # the flag would not change anything for them - the UI does not
            # offer it, and this refuses it rather than storing a value that
            # has no effect.
            user_id = int(request.form["user_id"])
            target = users.get_user(user_id)
            if target and target["is_admin"]:
                error = "Administrators already manage hypotheses."
            else:
                users.set_can_manage_hypotheses(
                    user_id, not (target and target["can_manage_hypotheses"]))
                message = "Hypothesis permissions updated."
        elif action == "toggle_active":
            user_id = int(request.form["user_id"])
            target = users.get_user(user_id)
            if target and target["id"] == users.current_user_id():
                error = "You cannot deactivate your own account."
            else:
                users.set_active(user_id, not (target and target["is_active"]))
                message = "Account updated."
    except users.UserError as exc:
        error = str(exc)
    except (KeyError, ValueError):
        error = "That request was incomplete."

    if error:
        # Re-rendered rather than redirected, so the reason stays on screen
        # next to the form that caused it.
        return render_template("users.html", users=users.list_users(),
                               current=users.current_user(), error=error,
                               message=None), 400
    # Post/Redirect/Get. Without it the browser holds on to the POST, and a
    # refresh re-submits it - which for toggle_admin or toggle_active means
    # silently flipping the flag straight back again.
    return redirect(url_for("auth.list_users", done=action if message else None))


def _admin_count():
    return sum(1 for u in users.list_users() if u["is_admin"] and u["is_active"])
