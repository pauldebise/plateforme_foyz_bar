"""Utilitaires : conversion monétaire stricte, normalisation, dates, formatage.

Aucune dépendance Flask : ce module doit rester importable seul.
"""

import contextlib
import html
import re
import unicodedata
from datetime import datetime, UTC
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .errors import MigrationError

PARIS_TZ = ZoneInfo("Europe/Paris")

_INT_RE = re.compile(r"^[+-]?\d+$")

_MONEY_ERROR = (
    "montant illisible : {!r} (les montants doivent être des décimaux en euros "
    "ou des entiers en centimes, sans devise)"
)


def to_cents(raw, unit="euros", context=""):
    """Convertit un montant source en centimes entiers (int), sans flottant.

    - unit="euros" : valeur décimale en euros (12.5, "12,50"…) -> 1250 centimes.
    - unit="cents" : valeur déjà en centimes, doit être un entier exact.
    Toute valeur non interprétable lève une MigrationError (pas d'arrondi silencieux).
    """
    prefix = f"[{context}] " if context else ""
    if raw is None or raw == "":
        return 0
    if isinstance(raw, bool):
        raise MigrationError(prefix + _MONEY_ERROR.format(raw))
    if isinstance(raw, int):
        return raw if unit == "cents" else raw * 100
    if isinstance(raw, float):
        # Interdit en source : on exige la représentation textuelle exacte pour
        # éviter tout artefact binaire avant même la conversion en centimes.
        raise MigrationError(prefix + _MONEY_ERROR.format(raw))
    if not isinstance(raw, (str, Decimal)):
        raise MigrationError(prefix + _MONEY_ERROR.format(raw))
    text = str(raw).strip().replace("\u00a0", "").replace("€", "").strip()
    if text == "":
        return 0
    try:
        value = Decimal(text.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        raise MigrationError(prefix + _MONEY_ERROR.format(raw)) from None
    if not value.is_finite():
        raise MigrationError(prefix + _MONEY_ERROR.format(raw))
    if unit == "cents":
        cents = value
    elif unit == "euros":
        cents = value * 100
    else:
        raise MigrationError(f"{prefix}unité monétaire inconnue : {unit!r}")
    integral = cents.to_integral_value()
    if cents != integral:
        raise MigrationError(
            f"{prefix}montant {text!r} comporte une fraction de centime impossible "
            f"à représenter exactement ({unit})"
        )
    return int(integral)


def normalize_key(value):
    """Clé de réconciliation : minuscule, sans accents/espaces superflus."""
    if value is None:
        return None
    text = unicodedata.normalize("NFKD", str(value).strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def slug_username(value):
    """Identifiant de connexion cible (users.username) : minuscule, sans
    accents, tout séparateur ramené à un point — ex : "Marie Claire Dupont" ->
    "marie.claire.dupont". Borné à 64 caractères. None si rien ne reste.

    Miroir strict de app.utils.slug_username (ce module reste volontairement
    sans dépendance Flask) : toute évolution doit être répercutée des deux
    côtés, sinon login et import généreraient des identifiants divergents.
    """
    if value is None:
        return None
    text = unicodedata.normalize("NFKD", str(value).strip().lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"['\u2019\u02bc]", "", text)  # apostrophes jointes : o'brien -> obrien
    text = re.sub(r"[^a-z0-9]+", ".", text).strip(".")
    return (text[:64].rstrip(".")) or None


def unescape_html(value):
    """Décode les entités HTML des chaînes sources (mysqldump &#x27; etc.)."""
    if value is None or not isinstance(value, str):
        return value
    try:
        return html.unescape(value)
    except Exception:
        return value


def liters_to_cl(value):
    """Volume source exprimé en litres (décimal) -> centilitres entiers.

    Retourne None si absent/illisible ; arrondi au centilitre le plus proche.
    """
    if value is None or value == "":
        return None
    try:
        liters = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation:
        return None
    if not liters.is_finite() or liters < 0:
        return None
    cl = (liters * 100).to_integral_value(rounding="ROUND_HALF_UP")
    return int(cl) or None


def parse_dt(raw, assume_tz=PARIS_TZ):
    """Parse une date source vers un datetime naive UTC (convention de la plateforme).

    Accepte : datetime, ISO 8601 (+ fuseau éventuel), "YYYY-MM-DD HH:MM:SS",
    "YYYY-MM-DD", epoch seconds. Une date naive source est supposée être
    l'heure locale du campus (Europe/Paris) et convertie en UTC.
    """
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        candidates = (text, text.replace("T", " "))
        dt = None
        for candidate in candidates:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(candidate, fmt)
                    break
                except ValueError:
                    continue
            if dt is not None:
                break
        if dt is None:
            with contextlib.suppress(ValueError):
                dt = datetime.fromisoformat(text)
        if dt is None:
            try:
                return datetime.fromtimestamp(int(float(text)), tz=UTC).replace(tzinfo=None)
            except (ValueError, OverflowError):
                raise MigrationError(f"date illisible : {raw!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt.astimezone(UTC).replace(tzinfo=None)


def utcnow():
    """Horodatage naive UTC (convention de la plateforme)."""
    return datetime.now(UTC).replace(tzinfo=None)


def fmt_euros(cents):
    """Formatage console : 12345 -> "123,45 €"."""
    if cents is None:
        return "0,00 €"
    sign = "-" if cents < 0 else ""
    return f"{sign}{abs(cents) // 100},{abs(cents) % 100:02d} €"


def looks_like_werkzeug_hash(value):
    """True si le hash est au format werkzeug (réutilisable tel quel en cible)."""
    if not value:
        return False
    return bool(re.match(r"^(scrypt|pbkdf2(?:-sha\d+)?|argon2|hex):", str(value)))
