"""Utilitaires : conversion monétaire stricte, normalisation, dates, formatage.

Aucune dépendance Flask : ce module doit rester importable seul.
"""

import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from .errors import MigrationError

PARIS_TZ = ZoneInfo("Europe/Paris")

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?\d+(?:[.,]\d+)?$")

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
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                pass
        if dt is None:
            try:
                return datetime.fromtimestamp(int(float(text)), tz=timezone.utc).replace(tzinfo=None)
            except (ValueError, OverflowError):
                raise MigrationError(f"date illisible : {raw!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=assume_tz)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


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
