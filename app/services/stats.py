import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import Float, case, cast, exists, func, literal, select

from app.extensions import db
from app.models import Contribution, Transaction, TransactionLine, User
from app.services.transactions import history_cutoff
from app.utils import ARTICLE_TYPES, to_paris, utcnow


def _fold(value):
    """Minuscule sans accents, pour une recherche insensible à la casse et
    aux diacritiques."""
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _parse_date(value, end_of_day=False):
    if not value:
        return None
    try:
        d = datetime.strptime(value, "%Y-%m-%d")
        return d + timedelta(days=1, microseconds=-1) if end_of_day else d
    except ValueError:
        return None


def _int(value):
    """Entier à partir d'un agrégat SQL (Decimal, float ou None)."""
    return int(value or 0)


def _match_user_scope(filters):
    """Restreint aux transactions comptant un participant correspondant aux
    filtres promotion/équipe, côté SQL (EXISTS) plutôt qu'en Python.

    Renvoie None (aucun filtre), un `false()` SQL (aucun résultat possible) ou
    une expression EXISTS."""
    promotion = filters.get("promotion")
    team_only = filters.get("team_only", "")
    if not promotion and not team_only:
        return None
    cond = (
        select(Contribution.id)
        .join(User, Contribution.user_id == User.id)
        .where(Contribution.transaction_id == Transaction.id)
    )
    if promotion:
        if not str(promotion).lstrip("-").isdigit():
            return db.false()
        cond = cond.where(User.promotion == int(promotion))
    if team_only == "team":
        cond = cond.where(User.team_status.is_not(None))
    elif team_only == "non_team":
        cond = cond.where(User.team_status.is_(None))
    return exists(cond)


def _apply_transaction_filters(stmt, filters, cutoff, types):
    """Filtres communs aux agrégats de ventes (type, campus, dates, portée
    utilisateur)."""
    stmt = stmt.where(
        Transaction.type.in_(types),
        Transaction.cancelled.is_(False),
        Transaction.created_at >= cutoff,
    )
    campus = filters.get("campus")
    if campus in ("brest", "paris"):
        stmt = stmt.where(Transaction.campus == campus)
    date_from = _parse_date(filters.get("date_from"))
    date_to = _parse_date(filters.get("date_to"), end_of_day=True)
    if date_from:
        stmt = stmt.where(Transaction.created_at >= date_from)
    if date_to:
        stmt = stmt.where(Transaction.created_at <= date_to)
    scope = _match_user_scope(filters)
    if scope is not None:
        stmt = stmt.where(scope)
    return stmt


def sales_stats(filters=None):
    filters = filters or {}
    cutoff = history_cutoff()
    category = filters.get("category")

    # Agrégats par catégorie et total : tout est calculé par la base, seuls
    # les compteurs remontent.
    agg = (
        select(
            TransactionLine.article_type,
            func.sum(TransactionLine.quantity),
            func.sum(TransactionLine.line_total),
        )
        .select_from(TransactionLine)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
    )
    agg = _apply_transaction_filters(agg, filters, cutoff, ("achat", "direct"))
    if category:
        agg = agg.where(TransactionLine.article_type == category)
    agg = agg.group_by(TransactionLine.article_type)

    totals = defaultdict(lambda: {"qty": 0, "revenue": 0})
    grand_total = {"qty": 0, "revenue": 0}
    for article_type, qty, revenue in db.session.execute(agg):
        totals[article_type] = {"qty": _int(qty), "revenue": _int(revenue)}
        grand_total["qty"] += _int(qty)
        grand_total["revenue"] += _int(revenue)

    # Série journalière : le découpage en jours de Paris dépend de l'heure
    # d'été/hiver, on ne peut pas le faire en SQL de façon portable. On
    # rapatrie donc uniquement (date, montant), sans objet ORM ni chargement
    # des lignes/participants, en flux pour borner la mémoire.
    series_stmt = (
        select(Transaction.created_at, TransactionLine.line_total)
        .select_from(TransactionLine)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
    )
    series_stmt = _apply_transaction_filters(series_stmt, filters, cutoff, ("achat", "direct"))
    if category:
        series_stmt = series_stmt.where(TransactionLine.article_type == category)
    series = defaultdict(int)
    for created_at, line_total in db.session.execute(series_stmt).yield_per(1000):
        series[to_paris(created_at).date().isoformat()] += line_total

    return {
        "grand_total": grand_total,
        "by_category": {k: v for k, v in sorted(totals.items(), key=lambda kv: -kv[1]["revenue"])},
        "series": dict(sorted(series.items())),
    }


def students_stats(filters=None, search="", page=1, per_page=0):
    filters = filters or {}
    cutoff = history_cutoff()
    category = filters.get("category")
    promotion = filters.get("promotion")
    team_only = filters.get("team_only", "")

    # Nombre de participants par transaction (ceux qui partagent le montant).
    participants = (
        select(Contribution.transaction_id, func.count().label("n"))
        .where(Contribution.user_id.isnot(None))
        .group_by(Contribution.transaction_id)
        .subquery()
    )
    # Montant et quantité des lignes retenues (filtre catégorie) par transaction.
    lines = select(
        TransactionLine.transaction_id.label("tid"),
        func.sum(TransactionLine.line_total).label("amount"),
        func.sum(TransactionLine.quantity).label("line_qty"),
    )
    if category:
        lines = lines.where(TransactionLine.article_type == category)
    lines = lines.group_by(TransactionLine.transaction_id).subquery()

    stmt = (
        select(
            User,
            func.count(Transaction.id).label("nb"),
            func.sum(cast(lines.c.amount, Float) / participants.c.n).label("spent"),
            func.sum(cast(lines.c.line_qty, Float) / participants.c.n).label("items"),
        )
        .select_from(Transaction)
        .join(Contribution, Contribution.transaction_id == Transaction.id)
        .join(User, Contribution.user_id == User.id)
        .join(lines, lines.c.tid == Transaction.id)
        .join(participants, participants.c.transaction_id == Transaction.id)
        .where(
            Transaction.type == "achat",
            Transaction.cancelled.is_(False),
            Transaction.created_at >= cutoff,
        )
        .group_by(User.id)
    )
    campus = filters.get("campus")
    if campus in ("brest", "paris"):
        stmt = stmt.where(Transaction.campus == campus)
    date_from = _parse_date(filters.get("date_from"))
    date_to = _parse_date(filters.get("date_to"), end_of_day=True)
    if date_from:
        stmt = stmt.where(Transaction.created_at >= date_from)
    if date_to:
        stmt = stmt.where(Transaction.created_at <= date_to)
    if promotion:
        if str(promotion).lstrip("-").isdigit():
            stmt = stmt.where(User.promotion == int(promotion))
        else:
            stmt = stmt.where(db.false())
    if team_only == "team":
        stmt = stmt.where(User.team_status.in_(["mandat", "ancien"]))
    elif team_only == "non_team":
        stmt = stmt.where(
            db.or_(User.team_status.is_(None), User.team_status.notin_(["mandat", "ancien"]))
        )

    result = []
    for user, nb, spent, items in db.session.execute(stmt):
        result.append(
            {
                "name": user.display_name,
                "promotion": user.promotion,
                "is_team": user.is_team,
                "nb": _int(nb),
                "articles": round(float(items or 0), 1),
                "spent": round(float(spent or 0)),
            }
        )
    result.sort(key=lambda s: -s["spent"])
    if search:
        needle = _fold(search)
        result = [s for s in result if needle in _fold(s["name"])]
    top = result[:10]
    if not per_page:
        return {"rows": result, "top": top, "total": len(result), "page": 1, "pages": 1}
    total = len(result)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    start = (page - 1) * per_page
    return {
        "rows": result[start : start + per_page],
        "top": top,
        "total": total,
        "page": page,
        "pages": pages,
    }


def top_articles_stats(filters=None, search=""):
    """Classement des articles les plus vendus sur la période filtrée.
    Regroupe par article (identifiant, sinon nom) et renvoie volume et
    recettes, triés par volume décroissant puis recettes."""
    filters = filters or {}
    cutoff = history_cutoff()
    category = filters.get("category")

    key = case(
        (
            TransactionLine.article_id.isnot(None),
            cast(TransactionLine.article_id, db.String),
        ),
        else_=literal("name:", type_=db.String) + TransactionLine.article_name,
    )
    stmt = (
        select(
            key.label("key"),
            func.max(TransactionLine.article_name),
            func.max(TransactionLine.article_type),
            func.sum(TransactionLine.quantity),
            func.sum(TransactionLine.line_total),
        )
        .select_from(TransactionLine)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
    )
    stmt = _apply_transaction_filters(stmt, filters, cutoff, ("achat", "direct"))
    if category:
        stmt = stmt.where(TransactionLine.article_type == category)
    stmt = stmt.group_by(key)

    articles = []
    for _key, name, article_type, qty, revenue in db.session.execute(stmt):
        articles.append(
            {
                "name": name,
                "type": article_type,
                "qty": _int(qty),
                "revenue": _int(revenue),
            }
        )
    if search:
        needle = _fold(search)
        articles = [
            a
            for a in articles
            if needle in _fold(a["name"])
            or needle in _fold(a["type"])
            or needle in _fold(ARTICLE_TYPES.get(a["type"], ""))
        ]
    articles.sort(key=lambda a: (-a["qty"], -a["revenue"]))
    return articles


def top_article_ids(campus=None, limit=None, days=45):
    """Classement des articles par popularité récente, comme le tri des
    étudiants : quantités vendues sur les `days` derniers jours (décroissant),
    puis date de la dernière vente (décroissante). Les articles sans vente
    récente n'apparaissent pas."""
    since = utcnow() - timedelta(days=days)
    stmt = (
        select(TransactionLine.article_id, func.sum(TransactionLine.quantity).label("qty"))
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .where(
            Transaction.type.in_(["achat", "direct"]),
            Transaction.cancelled.is_(False),
            Transaction.created_at >= since,
            TransactionLine.article_id.isnot(None),
        )
        .group_by(TransactionLine.article_id)
        .order_by(
            func.sum(TransactionLine.quantity).desc(),
            func.max(Transaction.created_at).desc(),
        )
    )
    if campus in ("brest", "paris"):
        stmt = stmt.where(Transaction.campus == campus)
    if limit:
        stmt = stmt.limit(limit)
    return [aid for aid in db.session.scalars(stmt)]
