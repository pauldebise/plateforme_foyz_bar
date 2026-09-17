import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
INSTANCE_DIR = BASE_DIR / "instance"
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", INSTANCE_DIR / "uploads"))

# Valeurs connues à refuser en production : si elles sont utilisées telles
# quelles, une session peut être forgée (SECRET_KEY) ou les opérations
# sensibles ouvertes à tous (ADMIN_PASSWORD).
INSECURE_SECRET_KEYS = {
    "dev-secret-key-change-me",
    "change-me-with-a-long-random-string",
}
INSECURE_ADMIN_PASSWORDS = {"admin", "admin123", "change-me"}


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-secret-key-change-me"
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{INSTANCE_DIR / 'foyz.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}
    UPLOAD_FOLDER = UPLOAD_DIR
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("HTTPS_ONLY", "0") == "1"
    LANGUAGES = ["fr"]
    TIMEZONE = "Europe/Paris"
    DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or "admin"


def validate_config(app):
    """Refuse les secrets par défaut hors développement.

    Une SECRET_KEY connue permet de forger un cookie de session (usurpation
    de n'importe quel compte, admin compris) ; un ADMIN_PASSWORD trivial ouvre
    les opérations sensibles (découvert, annulation). En production, ces
    valeurs sont bloquantes ; en développement elles ne sont que signalées.
    """
    problems = []
    secret = app.config.get("SECRET_KEY") or ""
    if secret in INSECURE_SECRET_KEYS or len(secret) < 32:
        problems.append(
            "SECRET_KEY absente, trop courte (32 caractères minimum) ou valeur connue "
            "(openssl rand -hex 32)"
        )
    admin = app.config.get("DEFAULT_ADMIN_PASSWORD") or ""
    if admin in INSECURE_ADMIN_PASSWORDS or not admin:
        problems.append("ADMIN_PASSWORD absente ou triviale")
    if not problems:
        return
    details = " ; ".join(problems)
    if app.config.get("DEBUG"):
        app.logger.warning(
            "Configuration de développement : %s (toléré hors production).", details
        )
        return
    raise RuntimeError(
        f"Configuration refusée : {details}. Renseignez les variables "
        "d'environnement (voir .env.example)."
    )


class DevConfig(Config):
    DEBUG = True


class ProdConfig(Config):
    DEBUG = False


def get_config():
    env = os.environ.get("FLASK_ENV", "production")
    return DevConfig if env == "development" else ProdConfig
