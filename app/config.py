import os
from pathlib import Path
from typing import ClassVar

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


def _env_int(name):
    try:
        return max(0, int(os.environ.get(name) or 0))
    except ValueError:
        return 0


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-secret-key-change-me"
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{INSTANCE_DIR / 'foyz.db'}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS: ClassVar[dict] = {"pool_pre_ping": True}
    UPLOAD_FOLDER = UPLOAD_DIR
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.environ.get("HTTPS_ONLY", "0") == "1"
    LANGUAGES: ClassVar[list[str]] = ["fr", "en"]
    BABEL_DEFAULT_LOCALE = "fr"
    BABEL_SUPPORTED_LOCALES: ClassVar[list[str]] = LANGUAGES
    BABEL_TRANSLATION_DIRECTORIES = str(BASE_DIR / "translations")
    TIMEZONE = "Europe/Paris"
    DEFAULT_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD") or "admin"
    # Version applicative affichée dans le tableau de bord (surchargée par
    # APP_VERSION au déploiement, par exemple le tag Git).
    APP_VERSION = os.environ.get("APP_VERSION") or "1.0"
    # Dossier des sauvegardes (lecture seule côté application : tableau de
    # bord). Les scripts ops utilisent la même variable d'environnement.
    BACKUP_DIR = Path(os.environ.get("BACKUP_DIR") or (BASE_DIR / "backups"))
    # Proxy de confiance devant l'application : "" (aucun) ou "cloudflare"
    # (l'IP client est alors lue dans CF-Connecting-IP au lieu de remote_addr).
    TRUSTED_PROXY = os.environ.get("TRUSTED_PROXY", "").strip().lower()
    # Nombre de proxys de confiance pour X-Forwarded-For (ProxyFix), 0 = désactivé.
    PROXY_FIX_X_FOR = _env_int("PROXY_FIX_X_FOR")


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
    if problems:
        details = " ; ".join(problems)
        if app.config.get("DEBUG"):
            app.logger.warning(
                "Configuration de développement : %s (toléré hors production).", details
            )
        else:
            raise RuntimeError(
                f"Configuration refusée : {details}. Renseignez les variables "
                "d'environnement (voir .env.example)."
            )
    if not app.config.get("DEBUG") and not app.config.get("SESSION_COOKIE_SECURE"):
        app.logger.warning(
            "HTTPS_ONLY=0 en production : le cookie de session n'est pas marqué "
            "« Secure » alors que l'URL publique est en HTTPS (voir README, §Cloudflare)."
        )


class DevConfig(Config):
    DEBUG = True


class ProdConfig(Config):
    DEBUG = False


def get_config():
    env = os.environ.get("FLASK_ENV", "production")
    return DevConfig if env == "development" else ProdConfig
