from datetime import datetime

from sqlalchemy import case, func, select

from app.extensions import db
from app.models import Transaction, TransactionLine
from app.services.transactions import history_cutoff
from app.utils import ARTICLE_TYPES, PAYMENT_METHODS, paris_to_utc, to_paris, utcnow


def _month_bounds(reference):
    start = reference.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        nxt = start.replace(year=start.year + 1, month=1)
    else:
        nxt = start.replace(month=start.month + 1)
    return start, nxt


def last_n_months(n=12):
    now = to_paris(utcnow()).replace(tzinfo=None)
    months = []
    for i in range(n - 1, -1, -1):
        y, m = now.year, now.month
        total = y * 12 + (m - 1) - i
        y, m = divmod(total, 12)
        m += 1
        months.append((y, m))
    return months


def _label_key(label):
    """« 2026-01 » -> (2026, 1) ; libellé NULL (hors fenêtre) -> None."""
    if label is None:
        return None
    year, month = label.split("-")
    return int(year), int(month)


def _month_case(months):
    """Expression SQL étiquetant chaque transaction par son mois civil de
    Paris, à partir des bornes UTC exactes (heure d'été/hiver comprise)."""
    whens = []
    for y, m in months:
        start, nxt = _month_bounds(datetime(y, m, 1))
        whens.append(
            (
                (Transaction.created_at >= paris_to_utc(start))
                & (Transaction.created_at < paris_to_utc(nxt)),
                f"{y}-{m:02d}",
            )
        )
    return case(*whens, else_=None)


def treasury(campus):
    campus = campus if campus in ("brest", "paris") else "brest"
    cutoff = history_cutoff()
    months = last_n_months(12)
    month_case = _month_case(months)

    report = []
    for y, m in months:
        report.append(
            {
                "year": y,
                "month": m,
                "label": f"{m:02d}/{y}",
                "reloads_total": 0,
                "reloads_by_method": {k: 0 for k in PAYMENT_METHODS},
                "sales_total": 0,
                "sales_by_type": {k: 0 for k in ARTICLE_TYPES},
                "events_total": 0,
            }
        )
    index = {(e["year"], e["month"]): e for e in report}

    reloads = (
        select(
            month_case.label("month"),
            Transaction.payment_method,
            func.sum(Transaction.total),
        )
        .where(
            Transaction.campus == campus,
            Transaction.cancelled.is_(False),
            Transaction.created_at >= cutoff,
            Transaction.type == "rechargement",
        )
        .group_by(month_case, Transaction.payment_method)
    )
    for label, method, total in db.session.execute(reloads):
        entry = index.get(_label_key(label))
        if entry is None:
            continue
        entry["reloads_total"] += int(total or 0)
        if method in entry["reloads_by_method"]:
            entry["reloads_by_method"][method] += int(total or 0)

    # Un seul passage : les lignes d'événement alimentent events_total, les
    # autres la consommation virtuelle (par type d'article).
    sales = (
        select(
            month_case.label("month"),
            TransactionLine.article_type,
            func.sum(case((Transaction.event_id.isnot(None), TransactionLine.line_total), else_=0)),
            func.sum(case((Transaction.event_id.is_(None), TransactionLine.line_total), else_=0)),
        )
        .select_from(TransactionLine)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .where(
            Transaction.campus == campus,
            Transaction.cancelled.is_(False),
            Transaction.created_at >= cutoff,
            Transaction.type.in_(["achat", "direct"]),
        )
        .group_by(month_case, TransactionLine.article_type)
    )
    for label, article_type, events, virtual in db.session.execute(sales):
        entry = index.get(_label_key(label))
        if entry is None:
            continue
        entry["events_total"] += int(events or 0)
        entry["sales_total"] += int(virtual or 0)
        if article_type in entry["sales_by_type"]:
            entry["sales_by_type"][article_type] += int(virtual or 0)

    for e in report:
        e["grand_total"] = e["reloads_total"] + e["events_total"]
    return report
