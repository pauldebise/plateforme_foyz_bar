"""Reclassement manuel des anciens articles qui faussent les statistiques.

Les lignes de vente conservent le nom et le type de l'article au moment de
l'achat. Les anciennes blagues et les compteurs de soirée n'ont pas toujours
été enregistrés avec le type « blague » : ils restent donc dans le classement
par volume. Ce module prépare la liste des articles à 0 € à examiner et
applique le type choisi par l'équipe, sans supprimer aucune ligne.

Aucun schéma n'est modifié : seule la colonne `transaction_lines.article_type`
des lignes de vente non annulées du campus est mise à jour.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict

from sqlalchemy import and_, case, func, or_, select, update

from app.extensions import db
from app.models import AuditLog, Transaction, TransactionLine
from app.utils import ARTICLE_TYPES, fold_text

BLAGUE_TYPE = "blague"
# Une ligne à 0 € de 100 unités ou plus est considérée comme un compteur
# (PAF, tarte au kk, ses morts…), pas comme un offert ordinaire.
SUSPECT_MIN_QTY = 100
SALE_TYPES = ("achat", "direct")


def _type_label(value):
    if not value:
        return "Non défini"
    return ARTICLE_TYPES.get(value, value)


def _type_order(value):
    if value in ARTICLE_TYPES:
        return (0, list(ARTICLE_TYPES).index(value))
    return (1, str(value))


def _token(key):
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _reviewed_keys(campus):
    """Articles déjà reclassés sur ce campus, d'après le journal d'audit.

    Le journal conserve le libellé affiché au moment de l'action. On le replie
    avec `fold_text` pour retrouver le groupe même si la casse a changé.
    """
    targets = db.session.scalars(
        select(AuditLog.target).where(
            AuditLog.action == "stats.reclassement_article",
            AuditLog.campus == campus,
        )
    ).all()
    return {fold_text(target).strip() for target in targets if target}


def _candidate_groups(campus):
    """Agrège les lignes de vente par nom d'article, insensible à la casse."""
    zero_price = TransactionLine.line_total == 0
    not_blague = or_(
        TransactionLine.article_type.is_(None),
        TransactionLine.article_type != BLAGUE_TYPE,
    )
    stmt = (
        select(
            TransactionLine.article_name,
            TransactionLine.article_type,
            func.count().label("lines"),
            func.sum(TransactionLine.quantity).label("qty"),
            func.sum(TransactionLine.line_total).label("revenue"),
            func.max(TransactionLine.quantity).label("max_qty"),
            func.min(Transaction.created_at).label("first_seen"),
            func.max(Transaction.created_at).label("last_seen"),
            func.sum(case((TransactionLine.article_id.is_(None), 1), else_=0)).label(
                "without_article"
            ),
            func.sum(case((zero_price, 1), else_=0)).label("zero_lines"),
            func.sum(case((zero_price, TransactionLine.quantity), else_=0)).label("zero_qty"),
            func.sum(case((and_(zero_price, not_blague), 1), else_=0)).label("zero_non_blague"),
            func.max(case((zero_price, TransactionLine.quantity), else_=None)).label(
                "zero_max_qty"
            ),
        )
        .select_from(TransactionLine)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .where(
            Transaction.campus == campus,
            Transaction.type.in_(SALE_TYPES),
            Transaction.cancelled.is_(False),
        )
        .group_by(TransactionLine.article_name, TransactionLine.article_type)
    )

    groups = {}
    for row in db.session.execute(stmt):
        name = row.article_name or ""
        key = fold_text(name).strip()
        if not key:
            continue
        group = groups.get(key)
        if group is None:
            group = {
                "key": key,
                "names": set(),
                "name_lines": defaultdict(int),
                "types": set(),
                "lines": 0,
                "qty": 0,
                "revenue": 0,
                "max_qty": 0,
                "zero_lines": 0,
                "zero_qty": 0,
                "zero_non_blague": 0,
                "zero_max_qty": 0,
                "without_article": 0,
                "first": None,
                "last": None,
            }
            groups[key] = group

        lines = int(row.lines or 0)
        group["names"].add(name)
        group["name_lines"][name] += lines
        group["types"].add(row.article_type)
        group["lines"] += lines
        group["qty"] += int(row.qty or 0)
        group["revenue"] += int(row.revenue or 0)
        group["max_qty"] = max(group["max_qty"], int(row.max_qty or 0))
        group["zero_lines"] += int(row.zero_lines or 0)
        group["zero_qty"] += int(row.zero_qty or 0)
        group["zero_non_blague"] += int(row.zero_non_blague or 0)
        group["zero_max_qty"] = max(group["zero_max_qty"], int(row.zero_max_qty or 0))
        group["without_article"] += int(row.without_article or 0)
        if row.first_seen is not None:
            group["first"] = (
                row.first_seen if group["first"] is None else min(group["first"], row.first_seen)
            )
        if row.last_seen is not None:
            group["last"] = (
                row.last_seen if group["last"] is None else max(group["last"], row.last_seen)
            )

    candidates = []
    for group in groups.values():
        # Seuls les articles qui ont au moins une ligne à 0 € sont proposés.
        # Le filtre « déjà blague » est appliqué par `candidates()` selon le
        # mode d'affichage, pour pouvoir corriger un reclassement précédent.
        if group["zero_lines"] <= 0:
            continue
        display_name = max(group["name_lines"], key=lambda name: (group["name_lines"][name], name))
        types = sorted(group["types"], key=_type_order)
        candidates.append(
            {
                "key": group["key"],
                "token": _token(group["key"]),
                "name": display_name,
                "names": sorted(group["names"]),
                "types": types,
                "type_labels": [_type_label(value) for value in types],
                "lines": group["lines"],
                "qty": group["qty"],
                "revenue": group["revenue"],
                "max_qty": group["max_qty"],
                "zero_lines": group["zero_lines"],
                "zero_qty": group["zero_qty"],
                "zero_non_blague": group["zero_non_blague"],
                "zero_max_qty": group["zero_max_qty"],
                "has_current": group["without_article"] < group["lines"],
                "has_old": group["without_article"] > 0,
                "first": group["first"],
                "last": group["last"],
            }
        )
    return candidates


def candidates(campus, search="", show_all=False, sort="qty"):
    """Liste les articles à examiner pour un campus.

    `show_all` inclut les lignes à 0 € de faible quantité (offerts, eaux…)
    et les articles déjà reclassés, pour pouvoir corriger un choix. Par défaut,
    seuls les compteurs d'au moins `SUSPECT_MIN_QTY` unités sur une ligne et
    jamais reclassés sont proposés.
    """
    needle = fold_text(search).strip()
    reviewed = set() if show_all else _reviewed_keys(campus)
    rows = []
    for candidate in _candidate_groups(campus):
        if candidate["zero_lines"] <= 0:
            continue
        if not show_all:
            if candidate["key"] in reviewed:
                continue
            if candidate["zero_non_blague"] <= 0:
                continue
            if candidate["zero_max_qty"] < SUSPECT_MIN_QTY:
                continue
        if needle:
            haystack = " ".join(
                [
                    candidate["key"],
                    *[fold_text(_type_label(value)) for value in candidate["types"]],
                ]
            )
            if needle not in haystack:
                continue
        rows.append(candidate)

    if sort == "nom":
        rows.sort(key=lambda item: item["key"])
    elif sort == "volume":
        rows.sort(key=lambda item: (-item["qty"], -item["zero_max_qty"], item["key"]))
    elif sort == "revenu":
        rows.sort(key=lambda item: (-item["revenue"], -item["qty"], item["key"]))
    else:
        rows.sort(key=lambda item: (-item["zero_max_qty"], -item["qty"], item["key"]))
    return rows


def reclassify_article(campus, candidate, new_type):
    """Applique `new_type` aux lignes de vente du campus pour cet article.

    Le groupe est identifié par les noms exacts collectés lors de la lecture
    (`candidate["names"]`), ce qui couvre les variantes de casse. Retourne le
    nombre de lignes effectivement modifiées (0 si elles avaient déjà ce type).
    """
    if new_type not in ARTICLE_TYPES:
        return 0
    names = list(candidate.get("names") or ())
    if not names:
        return 0

    transaction_ids = select(Transaction.id).where(
        Transaction.campus == campus,
        Transaction.type.in_(SALE_TYPES),
        Transaction.cancelled.is_(False),
    )
    stmt = (
        update(TransactionLine)
        .where(
            TransactionLine.article_name.in_(names),
            TransactionLine.transaction_id.in_(transaction_ids),
            or_(
                TransactionLine.article_type.is_(None),
                TransactionLine.article_type != new_type,
            ),
        )
        .values(article_type=new_type)
        .execution_options(synchronize_session=False)
    )
    result = db.session.execute(stmt)
    return max(0, int(result.rowcount or 0))
