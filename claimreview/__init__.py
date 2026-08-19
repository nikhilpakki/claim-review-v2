from flask import Flask

from config import Config, init_dirs

from . import root_state
from .db import init_db


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    init_dirs(config_class)
    init_db(app)

    from .routes import browse, claims, fetch, process, search_routes, documents, review, settings, rules

    app.register_blueprint(browse.bp)
    app.register_blueprint(claims.bp)
    app.register_blueprint(fetch.bp)
    app.register_blueprint(process.bp)
    app.register_blueprint(search_routes.bp)
    app.register_blueprint(documents.bp)
    app.register_blueprint(review.bp)
    app.register_blueprint(settings.bp)
    app.register_blueprint(rules.bp)

    with app.app_context():
        # A fetch runs in a daemon thread, so anything still marked 'running'
        # in the database died with the previous process.
        from .fetch import runs as fetch_runs
        fetch_runs.mark_interrupted_runs()

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
