"""Vérification du mot de passe administrateur d'un campus.

Ce secret est partagé par l'équipe et protège les opérations sensibles
(découvert, annulation, suppression de compte, retrait de blacklist). Pour
qu'il ne puisse pas être deviné en ligne :

- les échecs sont comptés **par campus** dans le journal d'audit (donc partagés
  entre les workers Gunicorn, sans table supplémentaire) : après
  `MAX_FAILURES` échecs sur `LOCK_WINDOW`, toute vérification est refusée
  jusqu'à ce que les plus anciens échecs sortent de la fenêtre ;
- chaque échec **et** chaque succès est journalisé (qui, quoi, depuis quelle IP).

Un mot de passe absent n'est pas une tentative : c'est la caisse qui demande
d'abord si le secret est nécessaire.
"""

from datetime import timedelta
from math import ceil

from sqlalchemy import select

from app.extensions import db
from app.models import AuditLog
from app.services import audit as A
from app.services.settings import check_admin_password
from app.utils import CAMPUSSES, utcnow

MAX_FAILURES = 5
LOCK_WINDOW = timedelta(minutes=15)

ACTION_FAILURE = "mot_de_passe_admin.echec"
ACTION_SUCCESS = "mot_de_passe_admin.succes"


class AdminPasswordLocked(Exception):
    """Trop d'échecs récents sur ce campus : vérification suspendue."""

    def __init__(self, minutes):
        self.minutes = minutes
        super().__init__(
            f"Trop d'échecs de mot de passe administrateur : réessayez dans {minutes} minute(s)."
        )


def _lock_remaining(campus):
    """Minutes restantes avant déblocage, ou 0 si le campus n'est pas bloqué."""
    now = utcnow()
    recent = db.session.scalars(
        select(AuditLog.created_at)
        .where(
            AuditLog.action == ACTION_FAILURE,
            AuditLog.campus == campus,
            AuditLog.created_at >= now - LOCK_WINDOW,
        )
        .order_by(AuditLog.created_at.desc())
        .limit(MAX_FAILURES)
    ).all()
    if len(recent) < MAX_FAILURES:
        return 0
    unlock_at = recent[-1] + LOCK_WINDOW
    return max(1, ceil((unlock_at - now).total_seconds() / 60))


def verify(password, campus, context):
    """True si le mot de passe du campus est correct, False sinon.

    Lève AdminPasswordLocked si le campus est bloqué. `context` décrit
    l'opération protégée (journal d'audit).

    - Succès : l'entrée d'audit est ajoutée à la session et validée avec
      l'opération qu'elle autorise (aucun commit ici : l'appelant peut tenir
      des verrous de portefeuille).
    - Échec : l'opération est refusée, donc son travail en attente est annulé
      (rollback) et l'échec est journalisé immédiatement.
    """
    if not password or campus not in CAMPUSSES:
        return False
    remaining = _lock_remaining(campus)
    if remaining:
        raise AdminPasswordLocked(remaining)
    if check_admin_password(password, campus):
        A.record(ACTION_SUCCESS, target=CAMPUSSES[campus], details=context, campus=campus)
        return True
    db.session.rollback()
    A.record(ACTION_FAILURE, target=CAMPUSSES[campus], details=context, campus=campus, commit=True)
    return False
