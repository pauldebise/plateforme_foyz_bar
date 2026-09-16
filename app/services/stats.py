import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, select

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


def sales_stats(filters=None):
    filters = filters or {}
    cutoff = history_cutoff()
    stmt = (
        select(TransactionLine, Transaction)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .where(
            Transaction.type.in_(["achat", "direct"]),
            Transaction.cancelled.is_(False),
            Transaction.created_at >= cutoff,
        )
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
    category = filters.get("category")
    if category:
        stmt = stmt.where(TransactionLine.article_type == category)

    rows = db.session.execute(stmt).all()

    matching_users = set()
    promotion = filters.get("promotion")
    team_only = filters.get("team_only", "")
    if promotion or team_only:
        cstmt = select(Contribution.user_id).join(User, Contribution.user_id == User.id)
        if promotion:
            try:
                cstmt = cstmt.where(User.promotion == int(promotion))
            except ValueError:
                cstmt = cstmt.where(db.false())
        if team_only == "team":
            cstmt = cstmt.where(User.team_status.is_not(None))
        elif team_only == "non_team":
            cstmt = cstmt.where(User.team_status.is_(None))
        matching_users = set(db.session.scalars(cstmt))

    totals = defaultdict(lambda: {"qty": 0, "revenue": 0})
    series = defaultdict(int)
    grand_total = {"qty": 0, "revenue": 0}
    for line, t in rows:
        if (promotion or team_only) and not any(
            c.user_id in matching_users for c in t.contributions
        ):
            continue
        totals[line.article_type]["qty"] += line.quantity
        totals[line.article_type]["revenue"] += line.line_total
        grand_total["qty"] += line.quantity
        grand_total["revenue"] += line.line_total
        day = to_paris(t.created_at).date().isoformat()
        series[day] += line.line_total

    return {
        "grand_total": dict(grand_total),
        "by_category": {k: dict(v) for k, v in sorted(totals.items(), key=lambda kv: -kv[1]["revenue"])},
        "series": dict(sorted(series.items())),
    }


def students_stats(filters=None, search="", page=1, per_page=0):
    filters = filters or {}
    cutoff = history_cutoff()
    stmt = select(Transaction).where(
        Transaction.type == "achat",
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

    category = filters.get("category")
    promotion = filters.get("promotion")
    team_only = filters.get("team_only", "")

    stats = {}
    for t in db.session.scalars(stmt).all():
        participants = [c.user for c in t.contributions if c.user_id and c.user]
        n = len(participants)
        if n == 0:
            continue
        lines = [l for l in t.lines if not category or l.article_type == category]
        if not lines:
            continue
        line_total = sum(l.line_total for l in lines)
        line_qty = sum(l.quantity for l in lines)
        for u in participants:
            e = stats.setdefault(u.id, {"user": u, "nb": 0, "items": 0.0, "spent": 0.0})
            e["nb"] += 1
            e["spent"] += line_total / n
            e["items"] += line_qty / n

    result = []
    for e in stats.values():
        u = e["user"]
        if promotion and str(u.promotion or "") != str(promotion):
            continue
        if team_only == "team" and not u.is_team:
            continue
        if team_only == "non_team" and u.is_team:
            continue
        result.append({
            "name": u.display_name,
            "promotion": u.promotion,
            "is_team": u.is_team,
            "nb": e["nb"],
            "articles": round(e["items"], 1),
            "spent": round(e["spent"]),
        })
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
        "rows": result[start:start + per_page],
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
    stmt = (
        select(TransactionLine, Transaction)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .where(
            Transaction.type.in_(["achat", "direct"]),
            Transaction.cancelled.is_(False),
            Transaction.created_at >= cutoff,
        )
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
    category = filters.get("category")
    if category:
        stmt = stmt.where(TransactionLine.article_type == category)

    rows = db.session.execute(stmt).all()

    matching_users = set()
    promotion = filters.get("promotion")
    team_only = filters.get("team_only", "")
    if promotion or team_only:
        cstmt = select(Contribution.user_id).join(User, Contribution.user_id == User.id)
        if promotion:
            try:
                cstmt = cstmt.where(User.promotion == int(promotion))
            except ValueError:
                cstmt = cstmt.where(db.false())
        if team_only == "team":
            cstmt = cstmt.where(User.team_status.is_not(None))
        elif team_only == "non_team":
            cstmt = cstmt.where(User.team_status.is_(None))
        matching_users = set(db.session.scalars(cstmt))

    articles = {}
    for line, t in rows:
        if (promotion or team_only) and not any(
            c.user_id in matching_users for c in t.contributions
        ):
            continue
        key = line.article_id if line.article_id is not None else f"name:{line.article_name}"
        entry = articles.setdefault(
            key,
            {"name": line.article_name, "type": line.article_type, "qty": 0, "revenue": 0},
        )
        entry["qty"] += line.quantity
        entry["revenue"] += line.line_total

    result = list(articles.values())
    if search:
        needle = _fold(search)
        result = [
            a for a in result
            if needle in _fold(a["name"])
            or needle in _fold(a["type"])
            or needle in _fold(ARTICLE_TYPES.get(a["type"], ""))
        ]
    result.sort(key=lambda a: (-a["qty"], -a["revenue"]))
    return result


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
