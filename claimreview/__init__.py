from flask import Flask

from config import Config, init_dirs

from . import root_state, users
from .db import init_db


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    init_dirs(config_class)
    init_db(app)

    from .routes import (auth, browse, claims, fetch, hypothesis, personal,
                         process, search_routes, documents, review, settings,
                         rules)

    app.register_blueprint(auth.bp)
    app.register_blueprint(browse.bp)
    app.register_blueprint(claims.bp)
    app.register_blueprint(fetch.bp)
    app.register_blueprint(process.bp)
    app.register_blueprint(search_routes.bp)
    app.register_blueprint(documents.bp)
    app.register_blueprint(review.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(rules.bp)
    app.register_blueprint(hypothesis.bp)
    app.register_blueprint(personal.bp)

    with app.app_context():
        # A fetch runs in a daemon thread, so anything still marked 'running'
        # in the database died with the previous process.
        from .fetch import runs as fetch_runs
        fetch_runs.mark_interrupted_runs()

        # A fresh deployment needs one account to sign in with; this does
        # nothing once any user exists.
        created = users.ensure_bootstrap_admin(
            app.config.get("BOOTSTRAP_ADMIN_USER"),
            app.config.get("BOOTSTRAP_ADMIN_PASSWORD"))
        if created:
            app.logger.warning(
                "Created initial administrator %r from BOOTSTRAP_ADMIN_USER. "
                "Change the password and remove it from the environment.",
                app.config.get("BOOTSTRAP_ADMIN_USER"))

    @app.before_request
    def require_login():
        """Closed by default: this holds patient data, so a page is reachable
        only from a signed-in session unless it is explicitly public."""
        from flask import request
        from .routes.auth import PUBLIC_ENDPOINTS
        endpoint = request.endpoint or ""
        if endpoint in PUBLIC_ENDPOINTS:
            return None
        if users.current_user() is None:
            from flask import jsonify, redirect, url_for
            # An API call gets JSON; only a page navigation gets a redirect,
            # so a polling request cannot silently receive the login HTML.
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not signed in"}), 401
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return None

    @app.template_filter("match_config")
    def _match_config_filter(config, kind="rule"):
        """A stored rule config (or a tag row, or nothing at all) as the
        settings the shared controls render. Nothing means a new one, which
        is how the two forms get their different defaults for the regex box
        without either template knowing what the default is."""
        from . import matching
        defaults = matching.TAG_DEFAULTS if kind == "tag" else matching.RULE_DEFAULTS
        return matching.normalize(config, defaults)

    @app.template_filter("match_summary")
    def _match_summary_filter(config):
        from . import matching
        return matching.describe(config)

    @app.template_filter("page_sections")
    def _page_sections_filter(sections):
        """A page's LAYOUT sections, trimmed, with the empty ones dropped.

        A section can arrive with a heading and no body, or a body and no
        heading (narrative continuing from the page before); only one with
        neither is nothing to show.
        """
        out = []
        for section in sections or []:
            title = (section.get("title") or "").strip()
            text = (section.get("text") or "").strip()
            if title or text:
                out.append({"title": title or "Untitled section", "text": text})
        return out

    @app.template_filter("scope_summary")
    def _scope_summary_filter(tags):
        """A rule's document scope, spelled out by the engine that enforces
        it - so the list cannot describe a narrowing the rule does not do."""
        from . import rules_engine
        return rules_engine.describe_scope(tags)

    @app.template_filter("extract_config")
    def _extract_config_filter(config):
        """A stored extraction config, or nothing at all for a new rule, with
        every key present - so the form never has to ask whether a setting
        exists before rendering its control."""
        from . import extraction
        return extraction.normalize(config)

    @app.template_filter("extract_summary")
    def _extract_summary_filter(config):
        from . import extraction
        return extraction.describe(config)

    @app.context_processor
    def inject_match_controls():
        """What the shared matching-controls block renders. Here rather than
        passed by each route, because the same block appears on the rule form
        and the tag form and the two must offer the same options."""
        from . import matching, settings_store
        try:
            default = settings_store.get_settings()["FUZZY_MATCH_THRESHOLD"]
        except Exception:
            default = 80
        return {"fuzzy_methods": matching.FUZZY_METHODS,
                "default_fuzzy_threshold": default}

    @app.context_processor
    def inject_user():
        return {"current_user": users.current_user()}

    @app.context_processor
    def inject_active_root():
        try:
            return {"active_root": root_state.get_active_root()}
        except RuntimeError:
            return {"active_root": None}

    @app.context_processor
    def inject_here_qs():
        """?next=<current path>, for the topbar's Rules/Settings links, so
        those pages can carry a way back to wherever the user actually was
        (e.g. a specific claim) instead of always landing on the claims
        list."""
        from flask import request
        try:
            from urllib.parse import quote
            return {"here_qs": "?next=" + quote(request.full_path.rstrip("?"), safe="")}
        except RuntimeError:
            return {"here_qs": ""}

    @app.route("/")
    def index():
        from flask import redirect, url_for
        return redirect(url_for("claims.list_claims_view"))

    return app
