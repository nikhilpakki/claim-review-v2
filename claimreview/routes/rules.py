from urllib.parse import quote

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

from .. import csv_data, procedure_codes, rules_engine

bp = Blueprint("rules", __name__)


def _safe_next():
    """The `next` query param, if it's a same-site path (never an absolute
    URL / protocol-relative `//host` - that would be an open redirect)."""
    value = request.args.get("next")
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return None


def _next_qs():
    """?next=... to append to this page's own form actions/links, so the
    return-to destination survives a POST-redirect-GET round trip."""
    value = _safe_next()
    return f"?next={quote(value, safe='')}" if value else ""


def _is_ajax():
    """True for the fetch-based submissions rules.html's JS makes (in place
    of a full-page form POST/GET) - lets each route serve both a plain HTML
    page (redirect/full render, for JS-disabled/no-JS fallback) and the
    AJAX "apply without leaving the page" flow (JSON fragments) from the
    same endpoint."""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _redirect_to_rules(**extra_args):
    return redirect(url_for("rules.view_rules", next=_safe_next(), **extra_args))


def _rules_context(**extra):
    return {
        "rules": rules_engine.list_rules(),
        "upload_meta": csv_data.get_upload_meta(),
        "available_fields": csv_data.get_available_fields(),
        "query_aliases": [q["Alias"] for q in current_app.config["DEFAULT_QUERIES"]],
        "procedure_codes": procedure_codes.list_procedure_codes(),
        "doc_scope_tags": rules_engine.DOC_SCOPE_TAGS,
        "edit_rule": None,
        "back_url": _safe_next() or url_for("claims.list_claims_view"),
        "next_qs": _next_qs(),
        **extra,
    }


def _fragments(status="ok", **extra):
    """The three page fragments (rule-builder form, configured-rules list,
    claims-data status), re-rendered server-side against current state and
    returned as JSON for rules.html's JS to swap into the DOM in place -
    same Jinja templates/logic as the full page, just returned piecemeal
    instead of requiring a full-page reload."""
    ctx = _rules_context(**extra)
    return jsonify({
        "status": status,
        "form_html": render_template("partials/rule_form.html", **ctx),
        "rules_list_html": render_template("partials/rules_list.html", **ctx),
        "csv_html": render_template("partials/csv_status.html", **ctx),
    })


def _procedure_codes_from_form(form):
    if form.get("pc_all") == "on":
        return ["All"]
    codes = form.getlist("pc_codes")
    return codes if codes else ["All"]


def _doc_scope_tags_from_form(form):
    tags = form.getlist("doc_scope_tags")
    return tags if tags else list(rules_engine.DEFAULT_DOC_SCOPE_TAGS)


# The scope dropdown submits one of these 4 combined values - split into
# the two axes rules_engine.py actually uses (doc-level x page-level).
_SCOPE_OPTIONS = {
    "any_page_any_doc": ("any_doc", "any_page"),
    "all_pages_any_doc": ("any_doc", "all_pages"),
    "any_page_all_docs": ("all_docs", "any_page"),
    "all_pages_all_docs": ("all_docs", "all_pages"),
}


def _parse_scope(value):
    return _SCOPE_OPTIONS.get(value, ("any_doc", "any_page"))


def _config_from_form(form, rule_type):
    if rule_type == "field_present":
        scope, page_scope = _parse_scope(form.get("fp_scope"))
        config = {
            "compare_with_csv": form.get("fp_compare_with_csv") == "on",
            "csv_field": (form.get("fp_csv_field") or "").strip(),
            "keyword": (form.get("fp_keyword") or "").strip(),
            "regex": form.get("fp_regex") == "on",
            "case_insensitive": form.get("fp_case_insensitive") == "on",
            "scope": scope,
            "page_scope": page_scope,
        }
    elif rule_type == "field_consistency":
        scope, page_scope = _parse_scope(form.get("fc_scope"))
        config = {
            "concept": form.get("fc_concept", ""),
            "compare_with_csv": form.get("fc_compare_with_csv") == "on",
            "csv_field": (form.get("fc_csv_field") or "").strip(),
            "skip_empty": form.get("fc_skip_empty") == "on",
            "scope": scope,
            "page_scope": page_scope,
        }
    elif rule_type == "length_of_stay":
        max_los = (form.get("los_max_days") or "").strip()
        config = {
            "csv_admission_field": (form.get("los_admission_field") or "admission_dt").strip(),
            "csv_discharge_field": (form.get("los_discharge_field") or "discharge_dt").strip(),
            "max_los_days": int(max_los) if max_los.isdigit() else None,
            "compare_with_extracted": form.get("los_compare_with_extracted") == "on",
        }
    elif rule_type == "documents_present":
        required = (form.get("dp_required_types") or "").replace("\r", "")
        config = {
            "check_declared": form.get("dp_check_declared") == "on",
            "required_types": [line.strip() for line in required.split("\n") if line.strip()],
            "check_investigation_attachments": form.get("dp_check_investigations") == "on",
            "flag_investigations_without_attachment": form.get("dp_flag_no_attachment") == "on",
        }
    else:
        return None
    config["procedure_codes"] = _procedure_codes_from_form(form)
    config["doc_scope_tags"] = _doc_scope_tags_from_form(form)
    return config


@bp.route("/rules")
def view_rules():
    if _is_ajax():
        return _fragments()
    return render_template("rules.html", **_rules_context())


@bp.route("/rules/<int:rule_id>/edit")
def edit_rule_form(rule_id):
    rule = rules_engine.get_rule(rule_id)
    if _is_ajax():
        return _fragments(edit_rule=rule)
    if not rule:
        return _redirect_to_rules()
    return render_template("rules.html", **_rules_context(edit_rule=rule))


@bp.route("/rules/upload", methods=["POST"])
def upload_csv():
    file = request.files.get("csv_file")
    if not file or not file.filename:
        error = "Choose a CSV file first."
        if _is_ajax():
            return _fragments(status="error", upload_error=error), 400
        return render_template("rules.html", **_rules_context(upload_error=error)), 400
    try:
        csv_data.upload_csv(file)
    except csv_data.CsvUploadError as exc:
        if _is_ajax():
            return _fragments(status="error", upload_error=str(exc)), 400
        return render_template("rules.html", **_rules_context(upload_error=str(exc))), 400

    if _is_ajax():
        return _fragments(status="saved")
    return _redirect_to_rules()


@bp.route("/rules/clear-data", methods=["POST"])
def clear_data():
    csv_data.clear_csv_data()
    if _is_ajax():
        return _fragments(status="saved")
    return _redirect_to_rules()


@bp.route("/rules/create", methods=["POST"])
def create_rule():
    name = (request.form.get("name") or "").strip()
    rule_type = request.form.get("rule_type")
    config = _config_from_form(request.form, rule_type)

    if config is None:
        error = "Choose a rule type."
        if _is_ajax():
            return _fragments(status="error", rule_error=error), 400
        return render_template("rules.html", **_rules_context(rule_error=error)), 400
    if not name:
        error = "Give the rule a name."
        if _is_ajax():
            return _fragments(status="error", rule_error=error), 400
        return render_template("rules.html", **_rules_context(rule_error=error)), 400

    rules_engine.create_rule(name, rule_type, config)
    if _is_ajax():
        return _fragments(status="saved")
    return _redirect_to_rules()


@bp.route("/rules/<int:rule_id>/update", methods=["POST"])
def update_rule(rule_id):
    rule = rules_engine.get_rule(rule_id)
    if not rule:
        if _is_ajax():
            return _fragments(status="error"), 404
        return _redirect_to_rules()

    name = (request.form.get("name") or "").strip()
    rule_type = request.form.get("rule_type")
    config = _config_from_form(request.form, rule_type)

    if config is None:
        error = "Choose a rule type."
        if _is_ajax():
            return _fragments(status="error", edit_rule=rule, rule_error=error), 400
        return render_template("rules.html", **_rules_context(edit_rule=rule, rule_error=error)), 400
    if not name:
        error = "Give the rule a name."
        if _is_ajax():
            return _fragments(status="error", edit_rule=rule, rule_error=error), 400
        return render_template("rules.html", **_rules_context(edit_rule=rule, rule_error=error)), 400

    rules_engine.update_rule(rule_id, name, rule_type, config)
    if _is_ajax():
        return _fragments(status="saved")
    return _redirect_to_rules()


@bp.route("/rules/<int:rule_id>/delete", methods=["POST"])
def delete_rule(rule_id):
    rules_engine.delete_rule(rule_id)
    if _is_ajax():
        return _fragments(status="saved")
    return _redirect_to_rules()


@bp.route("/rules/<int:rule_id>/toggle", methods=["POST"])
def toggle_rule(rule_id):
    rule = rules_engine.get_rule(rule_id)
    if rule:
        rules_engine.set_rule_enabled(rule_id, not rule["enabled"])
    if _is_ajax():
        return _fragments(status="saved")
    return _redirect_to_rules()
