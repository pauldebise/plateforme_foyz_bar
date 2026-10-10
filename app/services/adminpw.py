"""Vérification du mot de passe administrateur d'un campus.

Ce secret est partagé par l'équipe et protège les opérations sensibles
(découvert, annulation, suppression de compte, retrait de blacklist). Un campus
n'est **jamais bloqué** : un échec ne gêne pas l'équipe suivante en caisse. En
contrepartie, chaque échec **et** chaque succès est journalisé (qui, quelle
opération, depuis quelle IP) dans le journal d'audit.

Un mot de passe absent n'est pas une tentative : c'est la caisse qui demande
d'abord si le secret est nécessaire.
"""

from app.extensions import db
from app.services import audit as A
from app.services.settings import check_admin_password
from app.utils import CAMPUSSES

ACTION_FAILURE = "mot_de_passe_admin.echec"
ACTION_SUCCESS = "mot_de_passe_admin.succes"


def verify(password, campus, context):
    """True si le mot de passe du campus est correct, False sinon.

    `context` décrit l'opération protégée (journal d'audit).

    - Succès : l'entrée d'audit est ajoutée à la session et validée avec
      l'opération qu'elle autorise (aucun commit ici : l'appelant peut tenir
      des verrous de portefeuille).
    - Échec : l'opération est refusée, donc son travail en attente est annulé
      (rollback) et l'échec est journalisé immédiatement.
    """
    if not password or campus not in CAMPUSSES:
        return False
    if check_admin_password(password, campus):
        A.record(ACTION_SUCCESS, target=CAMPUSSES[campus], details=context, campus=campus)
        return True
    db.session.rollback()
    A.record(ACTION_FAILURE, target=CAMPUSSES[campus], details=context, campus=campus, commit=True)
    return False
