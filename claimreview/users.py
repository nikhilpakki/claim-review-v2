"""User accounts and the per-user preferences that hang off them.

The tool started single-user and much of it was global as a result: one active
folder, one claims dataset, one set of enabled rules. With several reviewers
sharing an instance, some of that has to be per person and some deliberately
must not be:

- Per user: which rules they have switched on, the folder they are reviewing,
  and their own uploaded claims dataset.
- Central, admin-only: the rules themselves, and the detection/Textract
  settings - those drive real AWS spend and shared cache contents, so they are
  not something each reviewer should be able to change for everyone.

Passwords are hashed with werkzeug (already a Flask dependency, no new
package). This is authentication for a tool inside a private VPC, not an
internet-facing service - there is no password reset flow, no lockout, and no
session expiry beyond the cookie.
"""
from datetime import datetime, timezone

from flask import g, session
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db

# owner_user_id used for rows that belong to everyone rather than one person
# (the claims data a fetch populates). Never a real user id: AUTOINCREMENT
# starts at 1.
SHARED_OWNER_ID = 0

SESSION_KEY = "user_id"


class UserError(ValueError):
    """A problem with the submitted account details, safe to show the user."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _row_to_user(row):
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "display_name": row["display_name"] or row["username"],
        "is_admin": bool(row["is_admin"]),
        "is_active": bool(row["is_active"]),
        # Admins always manage hypotheses; the column marks the reviewers who
        # have additionally been granted it.
        "can_manage_hypotheses": bool(row["is_admin"] or _column(row, "can_manage_hypotheses")),
    }


def _column(row, name, default=None):
    """A column that may not exist yet on an old row object."""
    try:
        return row[name]
    except (IndexError, KeyError):
        return default


def count_users():
    return get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0]


def get_user(user_id):
    return _row_to_user(
        get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def get_by_username(username):
    return get_db().execute(
        "SELECT * FROM users WHERE username=?", ((username or "").strip(),)).fetchone()


def list_users():
    return [_row_to_user(row) for row in get_db().execute(
        "SELECT * FROM users ORDER BY is_admin DESC, username")]


def create_user(username, password, display_name=None, is_admin=False):
    username = (username or "").strip()
    if not username:
        raise UserError("Username is required.")
    if len(password or "") < 8:
        raise UserError("Password must be at least 8 characters.")
    if get_by_username(username) is not None:
        raise UserError(f"User {username!r} already exists.")

    db = get_db()
    cursor = db.execute(
        "INSERT INTO users (username, password_hash, display_name, is_admin, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (username, generate_password_hash(password), (display_name or "").strip() or None,
         1 if is_admin else 0, _now()),
    )
    db.commit()
    return cursor.lastrowid


def set_password(user_id, password):
    if len(password or "") < 8:
        raise UserError("Password must be at least 8 characters.")
    db = get_db()
    db.execute("UPDATE users SET password_hash=? WHERE id=?",
               (generate_password_hash(password), user_id))
    db.commit()


def set_can_manage_hypotheses(user_id, allowed):
    """Grant or revoke hypothesis management for one reviewer.

    Has no effect on an admin, who manages hypotheses by virtue of being an
    admin - revoking the flag would not take the ability away, so the UI does
    not offer it for them.
    """
    db = get_db()
    db.execute("UPDATE users SET can_manage_hypotheses=? WHERE id=?",
               (1 if allowed else 0, user_id))
    db.commit()


def set_admin(user_id, is_admin):
    db = get_db()
    db.execute("UPDATE users SET is_admin=? WHERE id=?", (1 if is_admin else 0, user_id))
    db.commit()


def set_active(user_id, is_active):
    db = get_db()
    db.execute("UPDATE users SET is_active=? WHERE id=?", (1 if is_active else 0, user_id))
    db.commit()


def authenticate(username, password):
    """The user, or None. Deactivated accounts never authenticate."""
    row = get_by_username(username)
    if row is None or not row["is_active"]:
        return None
    if not check_password_hash(row["password_hash"], password or ""):
        return None
    db = get_db()
    db.execute("UPDATE users SET last_login_at=? WHERE id=?", (_now(), row["id"]))
    db.commit()
    return _row_to_user(row)


# ------------------------------------------------------------------ session


def log_in(user):
    session[SESSION_KEY] = user["id"]
    session.permanent = True
    g.pop("current_user", None)


def log_out():
    session.pop(SESSION_KEY, None)
    g.pop("current_user", None)


def current_user():
    """The signed-in user, or None. Cached on `g` for the request - several
    things per page ask who is looking."""
    if "current_user" in g:
        return g.current_user
    user_id = session.get(SESSION_KEY)
    user = get_user(user_id) if user_id else None
    if user is not None and not user["is_active"]:
        user = None
    g.current_user = user
    return user


def current_user_id():
    user = current_user()
    return user["id"] if user else None


# ------------------------------------------------------- per-user preferences


def get_active_root(user_id):
    row = get_db().execute(
        "SELECT active_root FROM user_state WHERE user_id=?", (user_id,)).fetchone()
    return row["active_root"] if row else None


def set_active_root(user_id, path):
    db = get_db()
    db.execute(
        "INSERT INTO user_state (user_id, active_root, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET active_root=excluded.active_root, "
        "updated_at=excluded.updated_at",
        (user_id, path, _now()),
    )
    db.commit()


def rule_preferences(user_id):
    """{rule_id: enabled} - only rules this user has explicitly overridden.

    A rule with no entry follows its own default, so a rule an admin adds is
    live for everyone immediately instead of staying invisible until each
    person opts in.
    """
    if not user_id:
        return {}
    return {row["rule_id"]: bool(row["enabled"]) for row in get_db().execute(
        "SELECT rule_id, enabled FROM user_rule_prefs WHERE user_id=?", (user_id,))}


def set_rule_preference(user_id, rule_id, enabled):
    db = get_db()
    db.execute(
        "INSERT INTO user_rule_prefs (user_id, rule_id, enabled, updated_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id, rule_id) DO UPDATE SET enabled=excluded.enabled, "
        "updated_at=excluded.updated_at",
        (user_id, rule_id, 1 if enabled else 0, _now()),
    )
    db.commit()


def clear_rule_preference(user_id, rule_id):
    """Drop the override so the rule follows its central default again."""
    db = get_db()
    db.execute("DELETE FROM user_rule_prefs WHERE user_id=? AND rule_id=?", (user_id, rule_id))
    db.commit()


def ensure_bootstrap_admin(username, password):
    """Create the first administrator if there are no users at all.

    Runs at startup from configuration so a fresh deployment is reachable
    without a console step. Does nothing once any account exists, so it cannot
    silently reset or re-add an administrator later.
    """
    if count_users() > 0:
        return None
    if not username or not password:
        return None
    return create_user(username, password, display_name=username, is_admin=True)
