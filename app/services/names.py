"""Noms seuls (sans surnom) pour les tableaux « sérieux » : historique et journal.

L'historique et le journal d'audit conservent un libellé figé « Nom (surnom) »
ou « Nom (surnom) (identifiant) » écrit au moment de l'opération. On retrouve
le nom seul en rapprochant ce libellé du compte correspondant ; si aucun
compte ne correspond (compte supprimé, libellé hérité), le libellé est laissé
tel quel. Aucune donnée stockée n'est modifiée.

`user_labels=True` s'utilise quand tous les libellés sont connus comme des
libellés de compte (acteur du journal, opérateur d'une vente, cible d'une
action sur un compte). Le nom réel est alors le préfixe avant la parenthèse,
même si le surnom ou l'identifiant a changé depuis l'écriture de la ligne.
"""

from sqlalchemy import select

from app.extensions import db
from app.models import User


def plain_names(labels, max_length, *, user_labels=False):
    """{libellé: nom seul} pour les libellés qui portent un surnom ou un identifiant.

    `max_length` est la taille de la colonne qui a stocké le libellé : un
    libellé de cette longueur peut avoir été tronqué. `user_labels` autorise
    la résolution par préfixe pour les libellés dont on sait qu'ils désignent
    un compte (voir la docstring du module)."""
    candidates = {}
    for label in {item for item in labels if item and " (" in item}:
        start = label.find(" (")
        while start != -1:
            candidates.setdefault(label[:start], set()).add(label)
            start = label.find(" (", start + 1)
    if not candidates:
        return {}
    users = db.session.execute(
        select(User.name, User.nickname, User.username).where(User.name.in_(list(candidates)))
    ).all()
    result = {}
    for name, nickname, username in users:
        forms = {f"{name} ({username})"}
        if nickname:
            forms.add(f"{name} ({nickname})")
            forms.add(f"{name} ({nickname}) ({username})")
        for label in candidates[name]:
            if user_labels or any(
                label == form or (len(label) == max_length and form.startswith(label))
                for form in forms
            ):
                result[label] = name
    return result
