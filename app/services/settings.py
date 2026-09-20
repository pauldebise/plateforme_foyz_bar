from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models import Setting
from app.utils import CAMPUSSES

DEFAULTS = {
    "overdraft_limit_cents": "500",
    "deposit_value_cents": "100",
    "deposit_enabled": "1",
    "regulation_pdf_brest": "",
    "regulation_pdf_paris": "",
    "homepage_text": "Bienvenue sur la plateforme des Foy'z & Bar de l'ENSTA.\nRetrouvez ici les événements de chaque campus, les prix du bar et les informations pratiques.",
    "theme_color_public": "#804db3",
    "theme_color_brest": "#41699c",
    "theme_color_paris": "#9c4c41",
    "logo_brest": "",
    "logo_paris": "",
    "payment_photo_brest": "",
    "payment_photo_paris": "",
    "max_history_days": "365",
    "login_logs_retention_days": "90",
    "audit_logs_retention_days": "365",
    "session_timeout_minutes": "30",
    "max_postits_private": "20",
    "max_postits_public": "10",
    # Mot de passe administrateur (opérations sensibles) : un par campus, saisi
    # et vérifié dans le contexte du campus de l'opération (voir
    # check_admin_password / set_admin_password).
    "admin_password_hash_brest": "",
    "admin_password_hash_paris": "",
    "link_hosting": "",
    "link_database": "",
    "link_repository": "",
    "site_name": "Foy'z & Bar",
}


def get_setting(key, default=None):
    row = db.session.get(Setting, key)
    if row is not None:
        return row.value
    return DEFAULTS.get(key, default if default is not None else "")


def set_setting(key, value):
    row = db.session.get(Setting, key)
    if row is None:
        row = Setting(key=key, value=str(value))
        db.session.add(row)
    else:
        row.value = str(value)
    db.session.flush()


def int_setting(key):
    try:
        return int(get_setting(key, "0"))
    except (TypeError, ValueError):
        return int(DEFAULTS.get(key, "0") or 0)


def bool_setting(key):
    return get_setting(key, "0") in ("1", "true", "True", "yes")


def overdraft_limit():
    return int_setting("overdraft_limit_cents")


def deposit_value():
    return int_setting("deposit_value_cents")


def deposit_enabled():
    return bool_setting("deposit_enabled")


def admin_password_key(campus):
    """Clé de réglage du mot de passe administrateur d'un campus."""
    return f"admin_password_hash_{campus}"


def check_admin_password(password, campus):
    """Vérifie le mot de passe administrateur du campus de l'opération.

    Chaque campus a son propre mot de passe (opérations sensibles : découvert,
    annulation, suppression de compte, retrait « blacklist alcool »). Un campus
    inconnu refuse la vérification.
    """
    if not password or campus not in CAMPUSSES:
        return False
    stored = get_setting(admin_password_key(campus), "")
    if stored:
        return check_password_hash(stored, password)
    # Repli en clair uniquement en développement (au démarrage, ensure_dev_admin
    # initialise le hash). En production, un hash absent refuse tout accès
    # plutôt que d'accepter une valeur par défaut.
    from flask import current_app

    if not current_app.config.get("DEBUG"):
        return False
    return password == current_app.config.get("DEFAULT_ADMIN_PASSWORD", "admin")


def set_admin_password(password, campus):
    set_setting(admin_password_key(campus), generate_password_hash(password))
