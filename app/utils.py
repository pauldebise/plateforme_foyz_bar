import ipaddress
import math
import re
import secrets
import unicodedata
from datetime import datetime, timezone
from functools import wraps
from zoneinfo import ZoneInfo

from flask import current_app, redirect, request, url_for, g

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


# Plafond des montants saisis (1 000 000,00 €) : borne les valeurs aberrantes
# et évite les débordements d'entiers sur les colonnes de prix.
MAX_CENTS = 100_000_000


def cents(value):
    """Convertit une saisie (« 12,50 ») en centimes entiers.

    Lève ValueError sur une valeur non numérique, `inf`/`nan` ou hors bornes :
    les appelants transforment l'erreur en message utilisateur plutôt que de
    laisser PostgreSQL lever une 500.
    """
    if value is None:
        return 0
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError) as exc:
        raise ValueError("montant invalide") from exc
    if not math.isfinite(number):
        raise ValueError("montant invalide")
    amount = int(round(number * 100))
    if abs(amount) > MAX_CENTS:
        raise ValueError("montant hors limites")
    return amount


def clamp_text(value, max_length):
    """Tronque une chaîne à la longueur de la colonne cible (SQLite tronque en
    silence, PostgreSQL rejette : on garde un comportement identique partout)."""
    if value is None:
        return None
    return str(value)[:max_length]


_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def safe_color(value, default="#804db3"):
    """N'accepte qu'une couleur hexadécimale `#rrggbb` (injection CSS sinon)."""
    value = (value or "").strip()
    return value if _HEX_COLOR_RE.match(value) else default


def slug_username(value):
    """Identifiant de connexion (users.username) : minuscule, sans accents,
    tout séparateur ramené à un point — ex : "Marie Claire Dupont" ->
    "marie.claire.dupont". Borné à 64 caractères. None si rien ne reste.

    Miroir strict de migration.util.slug_username (le module migration reste
    volontairement sans dépendance Flask) : toute évolution doit être répercutée
    des deux côtés, sinon login et import généreraient des identifiants divergents.
    """
    if value is None:
        return None
    text = unicodedata.normalize("NFKD", str(value).strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"['\u2019\u02bc]", "", text)  # apostrophes jointes : o'brien -> obrien
    text = re.sub(r"[^a-z0-9]+", ".", text).strip(".")
    return (text[:64].rstrip(".")) or None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not getattr(g, "current_user", None):
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped


def is_safe_target(target):
    return target and target.startswith("/") and not target.startswith("//")


def client_ip():
    """IP réelle du client (limiteur de connexion, journaux).

    `X-Forwarded-For` n'est jamais lu directement : un client peut le forger.
    `CF-Connecting-IP`, posé par Cloudflare, n'est retenu que si l'origine est
    déclarée exposée exclusivement via Cloudflare (`TRUSTED_PROXY=cloudflare` :
    pare-feu ou Tunnel empêchant l'accès direct). Sinon `remote_addr` fait foi,
    éventuellement corrigé en amont par ProxyFix (`PROXY_FIX_X_FOR`). La valeur
    est validée comme adresse IP et bornée à 64 caractères (LoginLog.ip).
    """
    candidates = []
    if current_app.config.get("TRUSTED_PROXY") == "cloudflare":
        candidates.append(request.headers.get("CF-Connecting-IP") or "")
    candidates.append(request.remote_addr or "")
    for candidate in candidates:
        try:
            return str(ipaddress.ip_address(candidate.strip()))[:64]
        except ValueError:
            continue
    return "?"
