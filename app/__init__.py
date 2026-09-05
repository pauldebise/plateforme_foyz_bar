import secrets
import time
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, current_app, g, jsonify, redirect, render_template, request, session, url_for, send_from_directory, abort
from sqlalchemy import func, select
from werkzeug.security import generate_password_hash

from app.config import get_config, UPLOAD_DIR
from app.extensions import db
from app.utils import CAMPUSSES, ARTICLE_TYPES, PAYMENT_METHODS, TRANSACTION_TYPES, euros, to_paris


def ensure_dev_admin():
    from app.models import User
    from app.services.settings import get_setting, set_admin_password

    password = current_app.config.get("DEFAULT_ADMIN_PASSWORD", "admin")
    admin = db.session.scalars(select(User).where(func.lower(User.name) == "admin")).first()
    if admin is None:
        admin = User(name="admin", team_status="mandat", team_campus="brest")
        db.session.add(admin)
    if not admin.password_hash:
        admin.password_hash = generate_password_hash(password)
    if not get_setting("admin_password_hash"):
        set_admin_password(password)
    db.session.commit()


def ensure_schema_upgrades():
    from sqlalchemy import inspect, text

    inspector = inspect(db.engine)
    table_columns = {
        t: {c["name"] for c in inspector.get_columns(t)}
        for t in inspector.get_table_names()
    }
    if "taps" in table_columns and "name" not in table_columns["taps"]:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE taps ADD COLUMN name VARCHAR(160)"))
    if "users" in table_columns and "legacy_password" not in table_columns["users"]:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN legacy_password VARCHAR(255)"))


def create_app():
    load_dotenv()
    app = Flask(__name__)
    app.config.from_object(get_config())

    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)

    from app.models import Setting, User  # noqa: F401

    with app.app_context():
        db.create_all()
        ensure_schema_upgrades()
        ensure_dev_admin()

    from app.routes.public import bp as public_bp
    from app.routes.auth import bp as auth_bp
    from app.routes.team import bp as team_bp
    from app.routes.admin import bp as admin_bp
    from app.routes.gateway import bp as gateway_bp
    from app.routes.api import bp as api_bp

    app.register_blueprint(public_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(team_bp, url_prefix="/equipe")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(gateway_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    @app.before_request
    def security_pipeline():
        rv = _csrf_check()
        if rv is not None:
            return rv
        rv = _session_timeout()
        if rv is not None:
            return rv
        _load_current_user()
        _resolve_scope()

    def _csrf_check():
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            token = session.get("_csrf_token")
            sent = request.form.get("_csrf") or request.headers.get("X-CSRFToken")
            if not token or not sent or not secrets.compare_digest(token, sent):
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, error="Jeton CSRF invalide."), 400
                abort(400, description="Jeton CSRF invalide, rechargez la page.")

    def _session_timeout():
        if not session.get("user_id") and not session.get("gateway_event_id"):
            return
        from app.services.settings import int_setting

        timeout_min = int_setting("session_timeout_minutes") or 30
        now = time.time()
        last = session.get("last_activity", now)
        if now - last > timeout_min * 60:
            session.clear()
            if request.path.startswith(("/api/", "/equipe", "/admin")):
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, error="Session expirée."), 401
            if request.path.startswith("/equipe") or request.path.startswith("/admin"):
                return redirect(url_for("auth.login", expired=1))
        session["last_activity"] = now
        session.permanent = True

    def _load_current_user():
        g.current_user = None
        if session.get("user_id"):
            from app.models import User

            u = db.session.get(User, session["user_id"])
            if u and u.is_team and not u.blacklist:
                g.current_user = u
            else:
                session.clear()

    def _resolve_scope():
        bp = request.blueprint or ""
        if bp in ("team", "admin", "api"):
            g.scope = "team"
        elif bp == "gateway":
            g.scope = "gateway"
        else:
            g.scope = "public"

    @app.teardown_appcontext
    def close_db(exc):
        pass

    @app.after_request
    def set_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    @app.template_filter("eur")
    def eur_filter(value):
        return euros(value)

    @app.template_filter("dt")
    def dt_filter(value, fmt="%d/%m/%Y %H:%M"):
        local = to_paris(value)
        return local.strftime(fmt) if local else ""

    @app.template_filter("dtdate")
    def dtdate_filter(value, fmt="%d/%m/%Y"):
        return dt_filter(value, fmt)

    @app.context_processor
    def inject_globals():
        from app.services.settings import get_setting, int_setting, bool_setting

        campus = session.get("campus") or ""
        return {
            "csrf_token": lambda: ensure_csrf(),
            "site_name": get_setting("site_name") or "Foy'z & Bar",
            "theme_color": (
                get_setting(f"theme_color_{campus}") if campus in CAMPUSSES
                else get_setting("theme_color_public")
            ) or "#804db3",
            "logo": get_setting(f"logo_{campus}") or "",
            "current_user": getattr(g, "current_user", None),
            "current_campus": session.get("campus", ""),
            "CAMPUSSES": CAMPUSSES,
            "ARTICLE_TYPES": ARTICLE_TYPES,
            "PAYMENT_METHODS": PAYMENT_METHODS,
            "TRANSACTION_TYPES": TRANSACTION_TYPES,
            "deposit_value": int_setting("deposit_value_cents"),
            "deposit_enabled": bool_setting("deposit_enabled"),
            "overdraft_limit": int_setting("overdraft_limit_cents"),
        }

    def ensure_csrf():
        if "_csrf_token" not in session:
            session["_csrf_token"] = secrets.token_hex(16)
        return session["_csrf_token"]

    @app.route("/uploads/<path:filename>")
    def uploaded_file(filename):
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        return render_template("errors/500.html"), 500

    @app.cli.command("init-db")
    def init_db_command():
        with app.app_context():
            db.create_all()
            ensure_dev_admin()
        print("Base de données initialisée.")

    return app
