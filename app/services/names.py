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

import re

from sqlalchemy import select

from app.extensions import db
from app.models import User


def _without_parentheticals(text):
    """Retire les groupes entre parenthèses, y compris imbriqués.

    Si les parenthèses sont déséquilibrées, le texte est renvoyé tel quel pour
    ne rien tronquer au hasard."""
    out = []
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif depth == 0:
            out.append(char)
    if depth:
        return text
    cleaned = " ".join("".join(out).split())
    return re.sub(r"\s+([,;])", r"\1", cleaned)


def plain_details(action, details):
    """Détails du journal sans surnom ni identifiant de compte.

    Les opérations protégées par le mot de passe administrateur inscrivent des
    libellés de comptes (« Nom (surnom) », « Nom (identifiant) ») dans leurs
    détails. Les autres détails gardent leurs parenthèses utiles (jeton d'API,
    campus, etc.)."""
    if not details or not action.startswith("mot_de_passe_admin."):
        return details
    if details.startswith("changement du mot de passe administrateur"):
        return details
    return _without_parentheticals(details)


def plain_names(labels, max_length, *, user_labels=False):
    """{libellé: nom seul} pour les libellés qui portent un surnom ou un identifiant.

    `max_length` est la taille de la colonne qui a stocké le libellé : un
    libellé de cette longueur peut avoir été tronqué. `user_labels` autorise
    la résolution par préfixe pour les libellés dont on sait qu'ils désignent
    un compte (voir la docstring du module)."""
    labels = {item for item in labels if item and " (" in item}
    candidates = {}
    username_labels = {}
    for label in labels:
        start = label.find(" (")
        while start != -1:
            candidates.setdefault(label[:start], set()).add(label)
            start = label.find(" (", start + 1)
        # Libellé de compte complet : le dernier segment entre parenthèses est
        # l'identifiant, qui reste unique même si le nom ou le surnom a changé.
        if user_labels and label.endswith(")"):
            username = label.rsplit(" (", 1)[1][:-1]
            if username:
                username_labels.setdefault(username, set()).add(label)
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
    if username_labels:
        renamed = db.session.execute(
            select(User.name, User.username).where(User.username.in_(list(username_labels)))
        ).all()
        for name, username in renamed:
            for label in username_labels[username]:
                result[label] = name
    return result
