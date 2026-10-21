"""Journal d'audit des actions d'administration (P10).

Chaque action sensible d'administration (comptes, équipe, articles, fûts,
tireuses, événements, réglages) est enregistrée avec son acteur, sa cible,
son campus et l'IP d'origine. Les entrées sont consultables dans l'onglet
« Actions » de `/admin/journal`. `audit_logs_retention_days` limite l'affichage
sans rien supprimer.

Le journal ne contient jamais de secret : ni mot de passe, ni jeton, ni
empreinte. Les messages sont rédigés pour être lisibles par un humain sans
avoir à croiser d'autres tables.
"""

from datetime import timedelta

from flask import g, has_request_context, session

from app.extensions import db
from app.models import AuditLog
from app.utils import clamp_text, client_ip, utcnow

ACTION_LABELS = {
    "compte.creation": "Création de compte",
    "compte.modification": "Modification de compte",
    "compte.suppression": "Suppression de compte",
    "equipe.modification": "Modification d'un membre d'équipe",
    "article.creation": "Création d'article",
    "article.modification": "Modification d'article",
    "article.desactivation": "Désactivation d'article",
    "fut.creation": "Enregistrement d'un fût",
    "fut.modification": "Modification d'un fût",
    "fut.suppression": "Suppression d'un fût",
    "tireuse.creation": "Ajout d'une tireuse",
    "tireuse.renommage": "Renommage d'une tireuse",
    "tireuse.affectation": "Affectation d'un fût",
    "tireuse.detachement": "Détachement d'un fût",
    "tireuse.suppression": "Suppression d'une tireuse",
    "evenement.creation": "Création d'événement",
    "evenement.modification": "Modification d'événement",
    "evenement.article_ajout": "Ajout d'un article d'événement",
    "evenement.article_suppression": "Suppression d'un article d'événement",
    "evenement.jeton": "Régénération du jeton d'événement",
    "evenement.fermeture": "Ouverture/fermeture d'un événement",
    "reglages.modification": "Modification des réglages",
    "reglages.couleurs": "Réinitialisation des couleurs",
    "reglages.mot_de_passe": "Changement du mot de passe administrateur",
    "mot_de_passe_admin.succes": "Mot de passe administrateur accepté",
    "mot_de_passe_admin.echec": "Mot de passe administrateur refusé",
    "lien.ajout": "Ajout d'un lien utile",
    "lien.suppression": "Suppression d'un lien utile",
    "api.jeton_creation": "Création d'un jeton API",
    "api.jeton_modification": "Modification d'un jeton API",
    "api.jeton_revocation": "Révocation d'un jeton API",
    "api.prix_article": "Modification de prix d'article via l'API",
    "api.prix_fut": "Modification de prix de fût via l'API",
}


def label(action):
    return ACTION_LABELS.get(action, action)


def record(action, target="", details="", *, commit=False, campus=None, actor_label=None):
    """Ajoute une entrée d'audit à la session courante.

    `target` désigne l'objet touché (nom, identifiant, libellé) ; `details`
    résume la modification. L'appelant commite (ou passe commit=True quand
    sa route ne fait aucun commit). Ne lève jamais pour un problème de
    contenu : les champs sont tronqués aux tailles de colonnes. `campus`
    force le campus de l'entrée (sinon celui de la session de l'acteur).
    `actor_label` remplace l'acteur de la session (appels par jeton d'API).
    """
    # Hors requête HTTP (commande CLI, service appelé directement) : pas
    # d'acteur ni d'IP, l'entrée est tout de même écrite.
    in_request = has_request_context()
    actor = getattr(g, "current_user", None) if in_request and actor_label is None else None
    entry = AuditLog(
        user_id=actor.id if actor else None,
        actor=clamp_text(actor_label or (actor.display_name if actor else ""), 160),
        campus=campus
        or (session.get("campus") if in_request else None)
        or (actor.team_campus if actor else "")
        or "",
        action=clamp_text(action, 60),
        target=clamp_text(target or "", 160),
        details=clamp_text(details or "", 2000),
        ip=client_ip() if in_request else "",
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry


def purge(days, dry_run=False):
    """Suppression manuelle (commande CLI) des entrées de plus de `days` jours ;
    retourne le compte. Jamais appelée automatiquement."""
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    query = db.session.query(AuditLog).filter(AuditLog.created_at < cutoff)
    if dry_run:
        return query.count()
    removed = query.delete(synchronize_session=False)
    db.session.commit()
    return removed
