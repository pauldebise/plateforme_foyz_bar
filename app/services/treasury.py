from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from app.extensions import db
from app.models import Transaction, TransactionLine
from app.services.transactions import history_cutoff
from app.utils import PAYMENT_METHODS, to_paris, utcnow, ARTICLE_TYPES


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


def treasury(campus):
    campus = campus if campus in ("brest", "paris") else "brest"
    cutoff = history_cutoff()
    rows = db.session.scalars(
        select(Transaction).where(
            Transaction.campus == campus,
            Transaction.cancelled.is_(False),
            Transaction.created_at >= cutoff,
            Transaction.type == "rechargement",
        )
    ).all()

    line_stmt = (
        select(TransactionLine, Transaction)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .where(
            Transaction.campus == campus,
            Transaction.cancelled.is_(False),
            Transaction.created_at >= cutoff,
            Transaction.type.in_(["achat", "direct"]),
        )
    )
    line_rows = db.session.execute(line_stmt).all()

    months = last_n_months(12)
    report = []
    for y, m in months:
        entry = {
            "year": y,
            "month": m,
            "label": f"{m:02d}/{y}",
            "reloads_total": 0,
            "reloads_by_method": {k: 0 for k in PAYMENT_METHODS},
            "sales_total": 0,
            "sales_by_type": {k: 0 for k in ARTICLE_TYPES},
            "events_total": 0,
        }
        report.append(entry)
    index = {(e["year"], e["month"]): e for e in report}

    for t in rows:
        local = to_paris(t.created_at)
        entry = index.get((local.year, local.month))
        if entry is None:
            continue
        entry["reloads_total"] += t.total
        if t.payment_method in entry["reloads_by_method"]:
            entry["reloads_by_method"][t.payment_method] += t.total

    for line, t in line_rows:
        local = to_paris(t.created_at)
        entry = index.get((local.year, local.month))
        if entry is None:
            continue
        if t.event_id:
            entry["events_total"] += line.line_total
        else:
            entry["sales_total"] += line.line_total
            if line.article_type in entry["sales_by_type"]:
                entry["sales_by_type"][line.article_type] += line.line_total

    for e in report:
        e["grand_total"] = e["reloads_total"] + e["events_total"]
    return report
