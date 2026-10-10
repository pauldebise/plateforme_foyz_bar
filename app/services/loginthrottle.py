"""Limiteur de connexion partagé entre les workers (table `login_throttle`).

Deux compteurs d'échecs, indépendants :

- par **identifiant** : 10 tentatives libres, puis un délai croissant (5 s,
  10 s, 20 s… plafonné à 15 min) avant chaque nouvel essai. Un mot de passe
  correct remet le compteur à zéro. Jamais de verrouillage définitif : un
  attaquant ne peut pas bloquer un compte pour de bon. Les échecs sont oubliés
  après 24 h sans nouvel échec ;
- par **adresse IP** (agrégée au /64 pour IPv6, sinon un client dispose de
  milliards d'adresses) : 30 échecs libres, même délai croissant, oubli après
  15 min. Seuls les échecs comptent : les connexions réussies d'un réseau
  partagé (école, bar) ne consomment pas le quota.

Les identifiants ne sont jamais stockés en clair : la clé est une empreinte.
"""

import hashlib
import ipaddress
from datetime import timedelta
from math import ceil

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import LoginThrottle
from app.utils import utcnow

ACCOUNT_FREE_ATTEMPTS = 10
IP_FREE_ATTEMPTS = 30
ACCOUNT_FORGET_AFTER = timedelta(hours=24)
IP_FORGET_AFTER = timedelta(minutes=15)
BASE_DELAY_SECONDS = 5
MAX_DELAY_SECONDS = 900

_RULES = {
    "acct": (ACCOUNT_FREE_ATTEMPTS, ACCOUNT_FORGET_AFTER),
    "ip": (IP_FREE_ATTEMPTS, IP_FORGET_AFTER),
}


def fingerprint(identifier):
    """Empreinte courte et non réversible d'un identifiant normalisé."""
    return hashlib.sha256((identifier or "").encode()).hexdigest()[:16]


def account_key(slug):
    return f"acct:{fingerprint(slug)}"


def ip_key(ip):
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "ip:?"
    if address.version == 6:
        return f"ip:{ipaddress.ip_network(f'{ip}/64', strict=False)}"
    return f"ip:{address}"


def delay_seconds(failures, free_attempts):
    """Délai imposé après `failures` échecs consécutifs (0 sous le seuil)."""
    if failures < free_attempts:
        return 0
    return min(MAX_DELAY_SECONDS, BASE_DELAY_SECONDS * 2 ** min(failures - free_attempts, 20))


def _rule(key):
    return _RULES[key.split(":", 1)[0]]


def remaining_seconds(*keys):
    """Secondes à attendre avant de pouvoir réessayer (0 si autorisé)."""
    now = utcnow()
    wait = 0
    for key in keys:
        row = db.session.get(LoginThrottle, key)
        if row is None:
            continue
        free, forget = _rule(key)
        if now - row.last_failure_at > forget:
            continue
        unlock = row.last_failure_at + timedelta(seconds=delay_seconds(row.failures, free))
        wait = max(wait, ceil((unlock - now).total_seconds()))
    return max(0, wait)


def record_failure(*keys):
    """Ajoute un échec à chaque clé (atomique, sûr entre workers) et valide."""
    now = utcnow()
    for key in keys:
        _, forget = _rule(key)
        stale = now - forget
        for _attempt in range(2):
            updated = db.session.execute(
                update(LoginThrottle)
                .where(LoginThrottle.key == key)
                .values(
                    failures=db.case(
                        (LoginThrottle.last_failure_at < stale, 1),
                        else_=LoginThrottle.failures + 1,
                    ),
                    last_failure_at=now,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if updated:
                break
            try:
                db.session.add(LoginThrottle(key=key, failures=1, last_failure_at=now))
                db.session.flush()
                break
            except IntegrityError:
                db.session.rollback()
    db.session.commit()


def reset(key):
    """Remet le compteur d'une clé à zéro (mot de passe correct)."""
    db.session.execute(
        db.delete(LoginThrottle)
        .where(LoginThrottle.key == key)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()


def reset_all():
    """Efface tous les compteurs (tests, déblocage manuel)."""
    db.session.execute(db.delete(LoginThrottle))
    db.session.commit()
