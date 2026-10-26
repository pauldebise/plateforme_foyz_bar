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
    # Décor de Noël : dates annuelles au format « MM-JJ » (sans année).
    "christmas_deco_start": "12-01",
    "christmas_deco_end": "12-31",
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


# Bornes des réglages numériques modifiables depuis l'interface : une valeur
# hors bornes est refusée (jamais enregistrée ni tronquée en silence).
SESSION_TIMEOUT_MIN_MINUTES = 5
INT_BOUNDS = {
    "max_history_days": (1, 3650),
    # 0 = pas de limite d'affichage
    "login_logs_retention_days": (0, 3650),
    "audit_logs_retention_days": (0, 3650),
    "session_timeout_minutes": (SESSION_TIMEOUT_MIN_MINUTES, 1440),
    "max_postits_private": (0, 1000),
    "max_postits_public": (0, 1000),
}
# Montants en centimes (saisis en euros) : (minimum, maximum)
CENTS_BOUNDS = {
    "overdraft_limit_cents": (0, 100_000),
    "deposit_value_cents": (0, 10_000),
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
    annulation, suppression de compte, retrait des statuts blacklist). Un campus
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


def session_timeout_minutes():
    """Durée d'inactivité avant déconnexion, jamais sous le plancher."""
    return max(SESSION_TIMEOUT_MIN_MINUTES, int_setting("session_timeout_minutes") or 30)


def display_cutoff(key):
    """Date au-delà de laquelle les journaux ne sont plus affichés, ou None.

    Les durées `login_logs_retention_days` et `audit_logs_retention_days`
    filtrent l'affichage sans rien supprimer : repasser de 1 à 365 jours fait
    réapparaître les entrées.
    """
    from datetime import timedelta

    from app.utils import utcnow

    days = int_setting(key)
    return utcnow() - timedelta(days=days) if days > 0 else None
