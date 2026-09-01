from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.models import Setting

DEFAULTS = {
    "overdraft_limit_cents": "500",
    "deposit_value_cents": "100",
    "deposit_enabled": "1",
    "regulation_pdf_brest": "",
    "regulation_pdf_paris": "",
    "homepage_text": "Bienvenue sur la plateforme des Foy'z & Bar de l'ENSTA.\nRetrouvez ici les événements de chaque campus, les prix du bar et les informations pratiques.",
    "theme_color_brest": "#00529c",
    "theme_color_paris": "#b02a37",
    "logo_brest": "",
    "logo_paris": "",
    "max_history_days": "365",
    "login_logs_retention_days": "90",
    "session_timeout_minutes": "30",
    "max_postits_private": "20",
    "max_postits_public": "10",
    "admin_password_hash": "",
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


def check_admin_password(password):
    if not password:
        return False
    stored = get_setting("admin_password_hash", "")
    if stored:
        return check_password_hash(stored, password)
    from flask import current_app

    return password == current_app.config.get("DEFAULT_ADMIN_PASSWORD", "admin")


def set_admin_password(password):
    set_setting("admin_password_hash", generate_password_hash(password))
