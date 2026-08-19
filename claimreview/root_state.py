import json
import os

from flask import current_app, session

_STATE_FILE_NAME = "last_root.json"


def _state_path():
    return os.path.join(current_app.config["CACHE_DIR"], _STATE_FILE_NAME)


def get_active_root():
    root = session.get("active_root")
    if root and os.path.isdir(root):
        return root

    # fall back to last-used root persisted on disk (survives cookie loss)
    path = _state_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                root = json.load(f).get("root")
        except (OSError, json.JSONDecodeError):
            root = None
        if root and os.path.isdir(root):
            session["active_root"] = root
            return root

    return current_app.config.get("DEFAULT_ROOT_DIR")


def set_active_root(path):
    session["active_root"] = path
    os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
    with open(_state_path(), "w", encoding="utf-8") as f:
        json.dump({"root": path}, f)


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
