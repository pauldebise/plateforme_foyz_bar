import secrets
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import redirect, url_for, g

PARIS_TZ = ZoneInfo("Europe/Paris")

CAMPUSSES = {"brest": "Brest", "paris": "Paris"}
PAYMENT_METHODS = {"cb": "Carte Bancaire", "lydia": "Lydia", "especes": "Espèces", "helloasso": "HelloAsso"}
ARTICLE_TYPES = {
    "biere": "Bière",
    "vin": "Vin",
    "cidre": "Cidre",
    "snack": "Snacks",
    "saucisson": "Saucisson",
    "evenement": "Évènement/Soirée",
}
ALCOHOL_TYPES = {"biere", "vin", "cidre"}
TRANSACTION_TYPES = {
    "achat": "Achat",
    "direct": "Paiement direct",
    "rechargement": "Rechargement",
    "retrait": "Retrait",
    "transfert": "Transfert",
    "consigne": "Retour de consigne",
}
TAP_SIZES = {"demi": ("Demi", 25), "pinte": ("Pinte", 50), "pot": ("Pot", 33)}


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_paris(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(PARIS_TZ)


def paris_to_utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=PARIS_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def new_token():
    return secrets.token_urlsafe(24)


def euros(cents):
    if cents is None:
        return "0,00 €"
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100},{abs(cents) % 100:02d} €"


def cents(value):
    if value is None:
        return 0
    return int(round(float(str(value).replace(",", ".")) * 100))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, "current_user", None):
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped


def is_safe_target(target):
    return target and target.startswith("/") and not target.startswith("//")
