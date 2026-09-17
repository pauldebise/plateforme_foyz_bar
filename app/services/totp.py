"""TOTP (RFC 6238) et codes de secours : MFA administrable (P10-5).

Implémentation autonome (hmac/hashlib/base64) : SHA-1, 6 chiffres, pas de
30 secondes — le format standard reconnu par Google Authenticator, Aegis,
FreeOTP, 1Password, etc.

Les codes de secours sont stockés hachés (werkzeug) et à usage unique : un
code consommé est retiré de la liste.
"""

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
from urllib.parse import quote, urlencode

from werkzeug.security import check_password_hash, generate_password_hash

STEP = 30
DIGITS = 6
WINDOW = 1
RECOVERY_COUNT = 8
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sans I, O, 0, 1


def generate_secret(length=20):
    """Secret base32 sans remplissage (160 bits par défaut)."""
    return base64.b32encode(secrets.token_bytes(length)).decode("ascii").rstrip("=")


def normalize_secret(secret):
    return "".join(ch for ch in (secret or "").upper() if ch.isalnum())


def _decode_secret(secret):
    cleaned = normalize_secret(secret)
    if not cleaned:
        raise ValueError("secret vide")
    padding = "=" * ((8 - len(cleaned) % 8) % 8)
    return base64.b32decode(cleaned + padding, casefold=True)


def code(secret, timestamp=None, step=STEP, digits=DIGITS):
    """Code TOTP pour un secret et un instant (secondes Unix)."""
    moment = time.time() if timestamp is None else timestamp
    counter = int(moment // step)
    digest = hmac.new(_decode_secret(secret), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10**digits)).zfill(digits)


def verify(secret, value, timestamp=None, window=WINDOW, last_counter=None):
    """Retourne le compteur consommé si le code est valide, sinon None.

    `last_counter` (anti-rejeu) refuse tout compteur déjà utilisé ou passé :
    un code intercepté ne peut pas resservir.
    """
    cleaned = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(cleaned) != DIGITS:
        return None
    moment = time.time() if timestamp is None else timestamp
    now = int(moment // STEP)
    for offset in range(-window, window + 1):
        counter = now + offset
        if last_counter is not None and counter <= last_counter:
            continue
        if hmac.compare_digest(code(secret, counter * STEP), cleaned):
            return counter
    return None


def provisioning_uri(secret, account, issuer="Foy'z & Bar"):
    """URI otpauth:// à saisir ou à encoder en QR par un client TOTP."""
    label = quote(f"{issuer}:{account}")
    params = urlencode(
        {
            "secret": normalize_secret(secret),
            "issuer": issuer,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": STEP,
        }
    )
    return f"otpauth://totp/{label}?{params}"


def _block():
    return "".join(secrets.choice(_ALPHABET) for _ in range(4))


def generate_recovery_codes(count=RECOVERY_COUNT):
    return [f"{_block()}-{_block()}" for _ in range(count)]


def hash_recovery_codes(codes):
    # Hachage de la forme normalisée : la saisie tolère tirets, espaces et casse.
    return json.dumps([generate_password_hash(normalize_recovery_code(value)) for value in codes])


def normalize_recovery_code(value):
    return "".join(ch for ch in (value or "").upper() if ch.isalnum())


def consume_recovery_code(stored, value):
    """Vérifie un code de secours et retourne (liste restante, valide).

    Le code valide est retiré : chaque code ne sert qu'une fois.
    """
    cleaned = normalize_recovery_code(value)
    if not cleaned or not stored:
        return stored, False
    try:
        hashes = json.loads(stored)
    except (TypeError, ValueError):
        return stored, False
    if not isinstance(hashes, list):
        return stored, False
    for index, hashed in enumerate(hashes):
        if check_password_hash(hashed, cleaned):
            remaining = hashes[:index] + hashes[index + 1 :]
            return json.dumps(remaining), True
    return stored, False


def remaining_recovery_codes(stored):
    try:
        hashes = json.loads(stored or "[]")
    except (TypeError, ValueError):
        return 0
    return len(hashes) if isinstance(hashes, list) else 0
