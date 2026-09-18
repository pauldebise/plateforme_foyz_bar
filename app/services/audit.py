"""Journal d'audit des actions d'administration (P10).

Chaque action sensible d'administration (comptes, équipe, articles, fûts,
tireuses, événements, réglages) est enregistrée avec son acteur, sa cible,
son campus et l'IP d'origine. Les entrées sont consultables dans
`/admin/audit` et purgées selon `audit_logs_retention_days`.

Le journal ne contient jamais de secret : ni mot de passe, ni jeton, ni
empreinte. Les messages sont rédigés pour être lisibles par un humain sans
avoir à croiser d'autres tables.
"""

from datetime import timedelta

from flask import g, session

from app.extensions import db
from app.models import AuditLog
from app.services.settings import int_setting
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
    "lien.ajout": "Ajout d'un lien utile",
    "lien.suppression": "Suppression d'un lien utile",
}


def label(action):
    return ACTION_LABELS.get(action, action)


def record(action, target="", details="", *, commit=False):
    """Ajoute une entrée d'audit à la session courante.

    `target` désigne l'objet touché (nom, identifiant, libellé) ; `details`
    résume la modification. L'appelant commite (ou passe commit=True quand
    sa route ne fait aucun commit). Ne lève jamais pour un problème de
    contenu : les champs sont tronqués aux tailles de colonnes.
    """
    actor = getattr(g, "current_user", None)
    entry = AuditLog(
        user_id=actor.id if actor else None,
        actor=clamp_text(actor.display_name, 160) if actor else "",
        campus=session.get("campus") or (actor.team_campus if actor else "") or "",
        action=clamp_text(action, 60),
        target=clamp_text(target or "", 160),
        details=clamp_text(details or "", 2000),
        ip=client_ip(),
    )
    db.session.add(entry)
    if commit:
        db.session.commit()
    return entry


def purge(retention_days=None, dry_run=False):
    """Supprime les entrées au-delà de la rétention ; retourne le compte."""
    days = int_setting("audit_logs_retention_days") if retention_days is None else retention_days
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    query = db.session.query(AuditLog).filter(AuditLog.created_at < cutoff)
    if dry_run:
        return query.count()
    removed = query.delete(synchronize_session=False)
    db.session.commit()
    return removed
