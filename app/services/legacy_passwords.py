"""Fin de vie des mots de passe hérités de l'ancienne plateforme.

La migration importe `users.legacy_password` brut (bcrypt, md5/sha1/sha256 ou
texte clair). À chaque connexion réussie, le compte est converti au format
werkzeug et le champ est vidé (cf. app.routes.auth). Ce module fournit
l'audit et la purge des comptes jamais reconnectés, une fois la campagne de
réinitialisation menée auprès des usagers.
"""

import re
from collections import Counter

from sqlalchemy import select

from app.extensions import db
from app.models import User

BCRYPT_HASH_RE = re.compile(r"^\$2[aby]\$\d{2}\$")
HEX_DIGESTS = {32: "md5", 40: "sha1", 64: "sha256"}
# Formats qu'une fuite de base expose directement (non salés ou en clair) :
# à purger en priorité.
WEAK_FORMATS = {"md5", "sha1", "sha256", "plaintext"}


def format_of(stored):
    """Décrit le format d'un mot de passe hérité (None si absent)."""
    if not stored:
        return None
    if BCRYPT_HASH_RE.match(stored):
        return "bcrypt"
    if len(stored) in HEX_DIGESTS and all(c in "0123456789abcdef" for c in stored.lower()):
        return HEX_DIGESTS[len(stored)]
    return "plaintext"


def accounts_with_legacy():
    return db.session.scalars(
        select(User).where(User.legacy_password.is_not(None), User.legacy_password != "")
    ).unique().all()


def audit():
    """Compte les comptes portant encore un mot de passe hérité, par format."""
    formats = Counter()
    for user in accounts_with_legacy():
        formats[format_of(user.legacy_password)] += 1
    return formats


def purge(weak_only=False):
    """Vide `legacy_password` (tous les comptes, ou seulement les formats
    faibles). Renvoie le nombre de comptes modifiés."""
    removed = 0
    for user in accounts_with_legacy():
        if weak_only and format_of(user.legacy_password) not in WEAK_FORMATS:
            continue
        user.legacy_password = None
        removed += 1
    db.session.commit()
    return removed
