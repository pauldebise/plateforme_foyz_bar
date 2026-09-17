import os
import secrets
import time
from pathlib import Path

import click
from dotenv import load_dotenv
from flask import (
    Flask,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    send_from_directory,
    abort,
)
from sqlalchemy import select
from werkzeug.security import generate_password_hash

from app.config import get_config, validate_config, INSTANCE_DIR
from app.logging_setup import configure_logging
from app.extensions import db
from app.utils import (
    ARTICLE_TYPES,
    CAMPUSSES,
    PAYMENT_METHODS,
    TRANSACTION_TYPES,
    euros,
    safe_color,
    to_paris,
)


def ensure_dev_admin():
    from app.models import User
    from app.services.settings import get_setting, set_admin_password

    password = current_app.config.get("DEFAULT_ADMIN_PASSWORD", "admin")
    admin = db.session.scalars(select(User).where(User.username == "admin")).first()
    if admin is None:
        admin = User(name="admin", username="admin", team_status="mandat", team_campus="brest")
        db.session.add(admin)
    if not admin.password_hash:
        admin.password_hash = generate_password_hash(password)
    if not get_setting("admin_password_hash"):
        set_admin_password(password)
    db.session.commit()


def ensure_schema_upgrades():
    from sqlalchemy import inspect, text

    from app.utils import slug_username

    inspector = inspect(db.engine)
    table_columns = {
        t: {c["name"] for c in inspector.get_columns(t)} for t in inspector.get_table_names()
    }
    if "taps" in table_columns and "name" not in table_columns["taps"]:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE taps ADD COLUMN name VARCHAR(160)"))
    if "users" in table_columns and "legacy_password" not in table_columns["users"]:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE users ADD COLUMN legacy_password VARCHAR(255)"))
    if "transactions" in table_columns and "idempotency_key" not in table_columns["transactions"]:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE transactions ADD COLUMN idempotency_key VARCHAR(64)"))
    if "transactions" in table_columns:
        with db.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ix_transactions_idempotency_key "
                    "ON transactions (idempotency_key)"
                )
            )
    if "users" in table_columns:
        cols = table_columns["users"]
        with db.engine.begin() as conn:
            if "username" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN username VARCHAR(64)"))
            if "nickname" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN nickname VARCHAR(255)"))
            if "disabled" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN disabled BOOLEAN DEFAULT FALSE"))
            if "trombinoscope_visible" not in cols:
                conn.execute(
                    text("ALTER TABLE users ADD COLUMN trombinoscope_visible BOOLEAN DEFAULT TRUE")
                )
            if "trombinoscope_role" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN trombinoscope_role VARCHAR(80)"))
            if "photo" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN photo VARCHAR(255)"))
            if "totp_secret" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN totp_secret VARCHAR(64)"))
            if "totp_enabled" not in cols:
                conn.execute(
                    text("ALTER TABLE users ADD COLUMN totp_enabled BOOLEAN DEFAULT FALSE")
                )
            if "totp_recovery" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN totp_recovery TEXT"))
            if "totp_last_counter" not in cols:
                conn.execute(text("ALTER TABLE users ADD COLUMN totp_last_counter INTEGER"))
        # Backfill des identifiants manquants (nouvelle colonne, ou comptes
        # créés hors app) : slug du nom, dédoublonné par suffixe numérique.
        with db.engine.begin() as conn:
            taken = {
                r[0]
                for r in conn.execute(
                    text("SELECT username FROM users WHERE username IS NOT NULL AND username != ''")
                )
            }
            for user_id, name in conn.execute(
                text("SELECT id, name FROM users WHERE username IS NULL OR username = ''")
            ).fetchall():
                base = slug_username(name) or "user"
                candidate, i = base, 1
                while candidate in taken:
                    i += 1
                    candidate = f"{base}{i}"
                taken.add(candidate)
                conn.execute(
                    text("UPDATE users SET username = :u WHERE id = :i"),
                    {"u": candidate, "i": user_id},
                )
        # L'unicité migre de name (désormais doublable : homonymes) vers username.
        indexes = {ix["name"]: ix for ix in inspector.get_indexes("users")}
        with db.engine.begin() as conn:
            if indexes.get("ix_users_name", {}).get("unique"):
                conn.execute(text("DROP INDEX ix_users_name"))
                conn.execute(text("CREATE INDEX ix_users_name ON users (name)"))
            if "ix_users_username" not in indexes:
                conn.execute(
                    text("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username)")
                )
    if "transaction_lines" in table_columns:
        indexes = {ix["name"] for ix in inspector.get_indexes("transaction_lines")}
        if "ix_transaction_lines_article_id" not in indexes:
            with db.engine.begin() as conn:
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_transaction_lines_article_id "
                        "ON transaction_lines (article_id)"
                    )
                )
    if "audit_logs" not in table_columns:
        with db.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS audit_logs ("
                    "id INTEGER NOT NULL PRIMARY KEY, "
                    "user_id INTEGER, "
                    "actor VARCHAR(160) NOT NULL DEFAULT '', "
                    "campus VARCHAR(10) NOT NULL DEFAULT '', "
                    "action VARCHAR(60) NOT NULL, "
                    "target VARCHAR(160) NOT NULL DEFAULT '', "
                    "details TEXT NOT NULL DEFAULT '', "
                    "ip VARCHAR(64) NOT NULL DEFAULT '', "
                    "created_at DATETIME NOT NULL, "
                    "FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE SET NULL)"
                )
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_audit_logs_action ON audit_logs (action)")
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at)"
                )
            )


def _restrict_instance_permissions(app):
    """SQLite : base en 600 et dossier `instance/` en 700 (D7).

    Ne touche jamais au dossier parent d'une base hors `instance/` (le
    durcissement d'un dossier système serait destructeur)."""
    from sqlalchemy.engine import make_url

    try:
        url = make_url(app.config["SQLALCHEMY_DATABASE_URI"])
    except Exception:
        return
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return
    db_path = Path(url.database)
    if not db_path.exists():
        return
    try:
        os.chmod(db_path, 0o600)
        if db_path.parent == INSTANCE_DIR:
            os.chmod(db_path.parent, 0o700)
    except OSError:
        app.logger.warning("Permissions inchangées pour %s", db_path)


def create_app():
    load_dotenv()
    app = Flask(__name__)
    app.config.from_object(get_config())
    configure_logging(app)
    validate_config(app)

    if app.config.get("PROXY_FIX_X_FOR", 0) > 0:
        # Nombre explicite de proxys de confiance : remote_addr devient l'IP
        # client issue de X-Forwarded-For (utile derrière Nginx local).
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=app.config["PROXY_FIX_X_FOR"])

    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)

    # Compression des réponses (Brotli puis gzip) : allège CSS/JS/HTML/JSON,
    # y compris en déploiement Docker sans Nginx devant l'application.
    from flask_compress import Compress

    Compress(app)
    app.config.setdefault("COMPRESS_ALGORITHM", ["br", "gzip"])
    app.config.setdefault("COMPRESS_MIN_SIZE", 500)

    from app.models import Setting, User  # noqa: F401

    with app.app_context():
        # SQLite (dev/tests) : schéma appliqué au démarrage. PostgreSQL
        # (production) : uniquement des révisions Alembic, via `init-db`
        # ou `upgrade-db` — aucun DDL à l'import de l'application.
        from app.schema import is_sqlite, tables_present

        if is_sqlite():
            db.create_all()
            ensure_schema_upgrades()
            _restrict_instance_permissions(app)
        if tables_present():
            ensure_dev_admin()
            from app.services.legacy_passwords import audit as legacy_audit

            remaining = legacy_audit()
            if remaining:
                app.logger.warning(
                    "Mots de passe hérités encore en base : %s. Lancer "
                    "`flask legacy-passwords --purge` une fois la campagne de "
                    "réinitialisation terminée.",
                    ", ".join(f"{fmt}:{count}" for fmt, count in sorted(remaining.items())),
                )

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
        _sync_campus()
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
        if (
            not session.get("user_id")
            and not session.get("gateway_event_id")
            and not session.get("mfa_user_id")
        ):
            return
        from app.services.settings import int_setting

        timeout_min = int_setting("session_timeout_minutes") or 30
        now = time.time()
        last = session.get("last_activity", now)
        if now - last > timeout_min * 60:
            session.clear()
            if request.path.startswith("/api/"):
                return jsonify(ok=False, error="Session expirée."), 401
            if request.path.startswith(("/equipe", "/admin")):
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

    def _sync_campus():
        """Le campus de travail est choisi à la connexion (l'autre campus que
        celui d'appartenance reste consultable en lecture seule). Ce fixup ne
        fait que garantir un campus valide si la session en est dépourvue."""
        u = getattr(g, "current_user", None)
        if u is None or session.get("campus") in CAMPUSSES:
            return
        session["campus"] = u.team_campus if u.team_campus in CAMPUSSES else "brest"

    def _resolve_scope():
        bp = request.blueprint or ""
        if bp in ("team", "admin", "api"):
            g.scope = "team"
        elif bp == "gateway":
            g.scope = "gateway"
        else:
            g.scope = "public"

    # Assets servis localement (Bootstrap, Icons, Chart.js) : plus aucune
    # dépendance CDN, donc une CSP stricte et un fonctionnement hors ligne.
    CSP = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-src 'self'; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )

    @app.after_request
    def set_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "same-origin"
        # setdefault : la route /uploads pose sa propre CSP plus restrictive
        # (default-src 'none') et ne doit pas être écrasée ici.
        response.headers.setdefault("Content-Security-Policy", CSP)
        path = request.path
        if path.startswith("/static/"):
            # Assets locaux (Bootstrap, Chart.js…) : cache navigateur explicite,
            # revalidation par ETag à l'expiration.
            response.headers["Cache-Control"] = "public, max-age=86400"
        elif path.startswith("/uploads/"):
            # Noms de fichiers horodatés (immuables en pratique).
            response.headers["Cache-Control"] = "public, max-age=86400"
        elif session.get("user_id") or session.get("gateway_event_id"):
            # Les pages authentifiées (équipe ou passerelle) ne doivent jamais
            # rester dans le cache du navigateur (navigation arrière après
            # déconnexion, poste partagé).
            response.headers["Cache-Control"] = "no-store, private"
        # HSTS uniquement quand l'application est déclarée servie en HTTPS
        # (HTTPS_ONLY=1) ; Cloudflare peut aussi le poser côté périphérie.
        if current_app.config.get("SESSION_COOKIE_SECURE"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
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
        current = getattr(g, "current_user", None)
        # campus d'appartenance du membre : seul campus où il peut écrire
        own = (
            current.team_campus
            if current is not None and current.team_campus in CAMPUSSES
            else campus
        )
        return {
            "csrf_token": lambda: ensure_csrf(),
            "site_name": get_setting("site_name") or "Foy'z & Bar",
            "theme_color": safe_color(
                get_setting(f"theme_color_{campus}")
                if campus in CAMPUSSES
                else get_setting("theme_color_public"),
            ),
            "logo": get_setting(f"logo_{campus}") or "",
            "payment_photo": (get_setting(f"payment_photo_{campus}") if campus in CAMPUSSES else "")
            or "",
            "current_user": getattr(g, "current_user", None),
            "current_campus": session.get("campus", ""),
            "own_campus": own,
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
        response = send_from_directory(app.config["UPLOAD_FOLDER"], filename)
        # Fichiers téléversés servis sur l'origine : on neutralise toute
        # exécution (un SVG/HTML résiduel ne pourrait rien charger ni exécuter).
        response.headers["Content-Security-Policy"] = "default-src 'none'"
        return response

    @app.get("/health")
    def health():
        """Sonde de supervision (D6) : base, uploads, version du schéma.

        Sans authentification et sans donnée sensible : un simple état. 503 si
        la base ou les téléversements sont indisponibles (le healthcheck Docker
        s'appuie dessus)."""
        from app.services import health as H

        checks = H.system_checks()
        degraded = "error" in (checks["database"], checks["uploads"])
        return jsonify(status="degraded" if degraded else "ok", **checks), (
            503 if degraded else 200
        )

    @app.errorhandler(403)
    def forbidden(e):
        return render_template("errors/403.html"), 403

    @app.errorhandler(404)
    def not_found(e):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def server_error(e):
        original = getattr(e, "original_exception", None)
        extra = {
            "event": "unhandled_error",
            "path": request.path[:255],
            "method": request.method,
            "endpoint": request.endpoint or "",
            "user_id": session.get("user_id"),
        }
        if original is not None:
            app.logger.error("unhandled_error", extra=extra, exc_info=original)
        else:
            app.logger.error("unhandled_error", extra=extra)
        return render_template("errors/500.html"), 500

    @app.cli.command("init-db")
    def init_db_command():
        """Prépare la base : schéma + compte administrateur initial."""
        from app.schema import is_sqlite, upgrade_to_head

        with app.app_context():
            if is_sqlite():
                db.create_all()
                ensure_schema_upgrades()
            else:
                upgrade_to_head()
            ensure_dev_admin()
        print("Base de données initialisée.")

    @app.cli.command("upgrade-db")
    def upgrade_db_command():
        """Applique les révisions Alembic en attente (production)."""
        from app.schema import upgrade_to_head

        with app.app_context():
            upgrade_to_head()
        print("Schéma à jour.")

    @app.cli.command("purge-logs")
    @click.option(
        "--days",
        type=int,
        default=None,
        help="Rétention en jours (défaut : réglage login_logs_retention_days).",
    )
    @click.option("--dry-run", is_flag=True, help="Affiche sans supprimer.")
    def purge_logs_command(days, dry_run):
        """Purge le registre des connexions au-delà de la rétention (D13)."""
        from datetime import timedelta

        from app.models import LoginLog
        from app.services.settings import int_setting
        from app.utils import utcnow

        with app.app_context():
            retention = days if days is not None else int_setting("login_logs_retention_days")
            if retention <= 0:
                print("Rétention désactivée (0) : rien à purger.")
                return
            cutoff = utcnow() - timedelta(days=retention)
            query = db.session.query(LoginLog).filter(LoginLog.created_at < cutoff)
            if dry_run:
                print(
                    f"{query.count()} entrée(s) antérieure(s) au {cutoff:%Y-%m-%d} seraient supprimées."
                )
                return
            removed = query.delete(synchronize_session=False)
            db.session.commit()
            print(f"{removed} entrée(s) supprimée(s) (antérieures au {cutoff:%Y-%m-%d}).")

    @app.cli.command("purge-audit")
    @click.option(
        "--days",
        type=int,
        default=None,
        help="Rétention en jours (défaut : réglage audit_logs_retention_days).",
    )
    @click.option("--dry-run", is_flag=True, help="Affiche sans supprimer.")
    def purge_audit_command(days, dry_run):
        """Purge le journal d'audit au-delà de la rétention."""
        from app.services import audit

        with app.app_context():
            removed = audit.purge(retention_days=days, dry_run=dry_run)
            if dry_run:
                print(f"{removed} entrée(s) d'audit seraient supprimées.")
            else:
                print(f"{removed} entrée(s) d'audit supprimée(s).")

    @app.cli.command("legacy-passwords")
    @click.option("--purge", is_flag=True, help="Vider legacy_password des comptes listés.")
    @click.option(
        "--weak-only",
        is_flag=True,
        help="Ne purger que les formats faibles (clair/md5/sha1/sha256).",
    )
    def legacy_passwords_command(purge, weak_only):
        """Audite (et éventuellement purge) les mots de passe hérités."""
        from app.services.legacy_passwords import (
            accounts_with_legacy,
            audit,
            format_of,
            purge as purge_legacy,
        )

        with app.app_context():
            users = accounts_with_legacy()
            if not users:
                print("Aucun mot de passe hérité en base.")
                return
            for user in users:
                print(f"{user.id}\t{user.username}\t{format_of(user.legacy_password)}")
            counts = audit()
            print("Total : " + ", ".join(f"{fmt}={n}" for fmt, n in sorted(counts.items())))
            if purge:
                removed = purge_legacy(weak_only=weak_only)
                print(
                    f"{removed} compte(s) purgé(s) — réinitialisation requise à la prochaine connexion."
                )

    return app
