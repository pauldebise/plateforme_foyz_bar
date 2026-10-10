import ipaddress
import math
import re
import secrets
import unicodedata
from datetime import datetime, UTC
from functools import wraps
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from flask import current_app, redirect, request, url_for, g

PARIS_TZ = ZoneInfo("Europe/Paris")

CAMPUSSES = {"brest": "Brest", "paris": "Paris"}
PAYMENT_METHODS = {
    "cb": "Carte Bancaire",
    "lydia": "Lydia",
    "especes": "Espèces",
    "helloasso": "HelloAsso",
}
ARTICLE_TYPES = {
    "biere": "Bière",
    "vin": "Vin",
    "cidre": "Cidre",
    "snack": "Snacks",
    "saucisson": "Saucisson",
    "evenement": "Évènement/Soirée",
    "blague": "Blague",
}
ALCOHOL_TYPES = {"biere", "vin", "cidre"}
# Types vendables (caisse, passerelle, statistiques, trésorerie) mais jamais
# affichés dans le catalogue public des prix : articles « pour rire ».
HIDDEN_PUBLIC_TYPES = {"blague"}
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
    return datetime.now(UTC).replace(tzinfo=None)


def to_paris(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=UTC).astimezone(PARIS_TZ)


def paris_to_utc(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=PARIS_TZ).astimezone(UTC).replace(tzinfo=None)


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

# Plafond d'une opération de caisse saisie à la main (rechargement, retrait,
# transfert) : 5 000,00 €.
MAX_OPERATION_CENTS = 500_000

_MAX_AMOUNT_INPUT_LENGTH = 32


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
    amount = round(number * 100)
    if abs(amount) > MAX_CENTS:
        raise ValueError("montant hors limites")
    return amount


def parse_amount(value, *, minimum=1, maximum=MAX_OPERATION_CENTS):
    """Montant saisi (« 12,50 ») -> centimes entiers bornés [minimum, maximum].

    Point d'entrée unique pour les rechargements, retraits et transferts : même
    arrondi que `cents()` (0,29 -> 29 centimes), refus de `inf`/`nan`/`1e999`,
    des booléens et des saisies démesurées. Lève ValueError, jamais d'autre
    exception : les appelants en font un message utilisateur ou une 400.
    """
    if isinstance(value, bool) or len(str(value)) > _MAX_AMOUNT_INPUT_LENGTH:
        raise ValueError("montant invalide")
    amount = cents(value)
    if amount < minimum or amount > maximum:
        raise ValueError("montant hors limites")
    return amount


_SECRET_PATH_RE = re.compile(r"(/passerelle/)[^/?#\s\"]+")


def redact_secrets(text):
    """Masque le jeton d'un lien passerelle (secret au porteur) dans un texte
    destiné aux journaux : « /passerelle/<jeton>/encaisser » -> « /passerelle/***/encaisser »."""
    return _SECRET_PATH_RE.sub(r"\1***", text) if isinstance(text, str) else text


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
    """Redirection interne uniquement : un chemin absolu du site, rien d'autre.

    Refuse les URL absolues ou sans schéma, le contre-oblique (les navigateurs
    le lisent comme une barre oblique : « /\\hote » vaut « //hote »), les
    caractères de contrôle (tabulations et sauts de ligne sont ignorés par les
    navigateurs, « /<tab>/hote » redevient « //hote ») et leurs variantes encodées.
    """
    if not target or not isinstance(target, str) or len(target) > 2048:
        return False
    if not target.startswith("/") or target[1:2] in ("/", "\\"):
        return False
    for candidate in (target, unquote(target)):
        if "\\" in candidate or any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
            return False
        if candidate.startswith("//"):
            return False
    parts = urlsplit(target)
    return not parts.scheme and not parts.netloc


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
