"""Noms seuls (sans surnom) pour les tableaux « sérieux » : historique et journal.

L'historique et le journal d'audit conservent un libellé figé « Nom (surnom) »
écrit au moment de l'opération. On retrouve le nom seul en rapprochant ce
libellé du nom d'affichage actuel d'un compte ; si aucun compte ne correspond
(compte supprimé, surnom modifié depuis, libellé hérité), le libellé est
laissé tel quel. Aucune donnée stockée n'est modifiée.
"""

from sqlalchemy import select

from app.extensions import db
from app.models import User


def plain_names(labels, max_length):
    """{libellé: nom seul} pour les libellés qui portent un surnom.

    `max_length` est la taille de la colonne qui a stocké le libellé : un
    libellé de cette longueur peut avoir été tronqué."""
    candidates = {}
    for label in {item for item in labels if item and " (" in item}:
        start = label.find(" (")
        while start != -1:
            candidates.setdefault(label[:start], set()).add(label)
            start = label.find(" (", start + 1)
    if not candidates:
        return {}
    users = db.session.execute(
        select(User.name, User.nickname).where(
            User.name.in_(list(candidates)), User.nickname.is_not(None)
        )
    ).all()
    result = {}
    for name, nickname in users:
        full = f"{name} ({nickname})"
        for label in candidates[name]:
            if label == full or (len(label) == max_length and full.startswith(label)):
                result[label] = name
    return result
