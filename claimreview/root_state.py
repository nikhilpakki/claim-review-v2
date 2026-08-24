"""The folder a reviewer is currently working in.

This used to be one global choice: the session held it, but the fallback was a
single last_root.json on disk, so whichever person changed folders last decided
where everyone else landed. With several reviewers it is per user, stored
against their account, and the shared file is only read as a one-time migration
for whoever signs in before setting a folder of their own.
"""
import json
import os

from flask import current_app, session

from . import users

_STATE_FILE_NAME = "last_root.json"


def _state_path():
    return os.path.join(current_app.config["CACHE_DIR"], _STATE_FILE_NAME)


def _legacy_root():
    """The pre-multi-user shared folder, used only as a starting suggestion."""
    path = _state_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f).get("root")
    except (OSError, json.JSONDecodeError):
        return None


def get_active_root():
    user_id = users.current_user_id()
    if user_id:
        root = users.get_active_root(user_id)
        if root and os.path.isdir(root):
            return root

    # Not chosen yet: fall back to the session, then to the folder this
    # instance used before it had accounts, then to the configured default.
    root = session.get("active_root")
    if root and os.path.isdir(root):
        return root
    root = _legacy_root()
    if root and os.path.isdir(root):
        return root
    return current_app.config.get("DEFAULT_ROOT_DIR")


def set_active_root(path):
    """Remember this folder for the signed-in user only."""
    session["active_root"] = path
    user_id = users.current_user_id()
    if user_id:
        users.set_active_root(user_id, path)


def get_claim_path(claim_id):
    root = get_active_root()
    if not root:
        raise ValueError("No root folder selected")
    claim_path = os.path.normpath(os.path.join(root, claim_id))
    root_norm = os.path.normpath(root)
    if not (claim_path == root_norm or claim_path.startswith(root_norm + os.sep)):
        raise ValueError("Invalid claim id")
    if not os.path.isdir(claim_path):
        raise FileNotFoundError(claim_id)
    return claim_path
