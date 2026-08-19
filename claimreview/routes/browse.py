import os

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from .. import fs_browser, root_state

bp = Blueprint("browse", __name__)


@bp.route("/browse")
def browse():
    path = request.args.get("path") or root_state.get_active_root() or ""
    as_json = request.args.get("format") == "json"

    if not path:
        listing = {"path": None, "parent": None, "entries": fs_browser.list_drives()}
    else:
        try:
            listing = fs_browser.list_dir(path)
        except (NotADirectoryError, FileNotFoundError, PermissionError, OSError) as exc:
            if as_json:
                return jsonify({"error": str(exc)}), 400
            listing = {"path": None, "parent": None, "entries": fs_browser.list_drives()}

    if as_json:
        return jsonify(listing)

    return render_template("browse.html", listing=listing,
                            active_root=root_state.get_active_root())


@bp.route("/browse/select-root", methods=["POST"])
def select_root():
    if request.is_json:
        path = (request.json or {}).get("path")
    else:
        path = request.form.get("path")
    if not path or not os.path.isdir(path):
        return jsonify({"error": "Not a valid directory"}), 400
    root_state.set_active_root(os.path.normpath(path))
    return redirect(url_for("claims.list_claims_view"))
