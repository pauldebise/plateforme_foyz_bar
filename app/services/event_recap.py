"""Récapitulatif financier d'un événement terminé ou clôturé.

Les ventes sont rattachées à l'événement de deux façons : par
``transactions.event_id`` (passerelle et paniers entièrement rattachables) et
par les lignes dont l'article appartient à l'événement (paniers multi-
événements non rattachables). Les montants et volumes sont calculés en SQL
autant que possible ; seules les répartitions horaires (heure de Paris, heure
d'été/hiver) et les agrégations par fût sont faites en Python.
"""

from sqlalchemy import case, cast, exists, func, literal, or_, select
from sqlalchemy.orm import selectinload

from app.extensions import db
from app.models import Article, Contribution, Keg, Transaction, TransactionLine, User
from app.services.names import plain_names
from app.utils import ARTICLE_TYPES, PAYMENT_METHODS, to_paris

SALE_TYPES = ("achat", "direct")
GATEWAY_PREFIX = "Passerelle · "


def _line_scope(event):
    """Ligne comptée dans l'événement : transaction rattachée entièrement, ou
    ligne d'un article temporaire de l'événement (panier multi-événements)."""
    return or_(
        Transaction.event_id == event.id,
        TransactionLine.article_id.in_(select(Article.id).where(Article.event_id == event.id)),
    )


def _deposit(row, event_id):
    """Part « consignes » d'une transaction rattachée à l'événement.

    Pour un panier multi-événements (non rattaché), la consigne ne peut pas
    être attribuée à un seul événement : elle est donc laissée hors du récap.
    """
    if row["event_id"] != event_id:
        return 0
    return max(0, int(row["total"] or 0) - int(row["lines_total"] or 0))


def _sales_rows(event):
    """Une ligne par transaction de vente, avec le montant des lignes retenues.

    Les transactions annulées sont conservées pour être comptées à part ; les
    totaux financiers ne les incluent jamais.
    """
    rows = db.session.execute(
        select(
            Transaction.id,
            Transaction.created_at,
            Transaction.total,
            Transaction.operator_label,
            Transaction.payment_method,
            Transaction.event_id,
            Transaction.cancelled,
            func.sum(TransactionLine.line_total).label("lines_total"),
            func.sum(TransactionLine.quantity).label("items"),
        )
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .where(Transaction.type.in_(SALE_TYPES), _line_scope(event))
        .group_by(
            Transaction.id,
            Transaction.created_at,
            Transaction.total,
            Transaction.operator_label,
            Transaction.payment_method,
            Transaction.event_id,
            Transaction.cancelled,
        )
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
    ).all()
    return [
        {
            "id": row.id,
            "created_at": row.created_at,
            "total": int(row.total or 0),
            "operator_label": row.operator_label or "",
            "payment_method": row.payment_method,
            "event_id": row.event_id,
            "cancelled": bool(row.cancelled),
            "lines_total": int(row.lines_total or 0),
            "items": int(row.items or 0),
        }
        for row in rows
    ]


def _article_rows(event):
    """Détail par article vendu (identifiant, sinon nom pour un article
    supprimé), avec volume et fût associés quand ils existent."""
    key = case(
        (
            TransactionLine.article_id.isnot(None),
            cast(TransactionLine.article_id, db.String),
        ),
        else_=literal("name:", type_=db.String) + TransactionLine.article_name,
    )
    rows = db.session.execute(
        select(
            key.label("key"),
            func.max(TransactionLine.article_name).label("name"),
            func.max(TransactionLine.article_type).label("article_type"),
            func.sum(TransactionLine.quantity).label("qty"),
            func.sum(TransactionLine.line_total).label("revenue"),
            func.count(func.distinct(Transaction.id)).label("sales"),
            func.max(Article.volume_cl).label("volume_cl"),
            func.max(Article.keg_id).label("keg_id"),
            func.max(Keg.name).label("keg_name"),
            func.max(Keg.volume_l).label("keg_volume_l"),
        )
        .select_from(TransactionLine)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .outerjoin(Article, TransactionLine.article_id == Article.id)
        .outerjoin(Keg, Article.keg_id == Keg.id)
        .where(
            Transaction.type.in_(SALE_TYPES),
            Transaction.cancelled.is_(False),
            _line_scope(event),
        )
        .group_by(key)
    ).all()
    return [
        {
            "name": row.name or "Article supprimé",
            "article_type": row.article_type or "",
            "qty": int(row.qty or 0),
            "revenue": int(row.revenue or 0),
            "sales": int(row.sales or 0),
            "volume_cl": row.volume_cl,
            "keg_id": row.keg_id,
            "keg_name": row.keg_name,
            "keg_volume_l": float(row.keg_volume_l or 0),
        }
        for row in rows
    ]


def _buyer_match(event):
    event_line = exists(
        select(TransactionLine.id).where(
            TransactionLine.transaction_id == Transaction.id,
            TransactionLine.article_id.in_(select(Article.id).where(Article.event_id == event.id)),
        )
    )
    return or_(Transaction.event_id == event.id, event_line)


def _buyer_stats(event):
    match = _buyer_match(event)
    base = (
        select(Contribution.id)
        .join(Transaction, Contribution.transaction_id == Transaction.id)
        .where(
            Contribution.user_id.isnot(None),
            Transaction.type.in_(SALE_TYPES),
            Transaction.cancelled.is_(False),
            match,
        )
    )
    unique = db.session.scalar(
        select(func.count(func.distinct(Contribution.user_id))).where(Contribution.id.in_(base))
    )
    top = db.session.execute(
        select(
            User.id,
            User.name,
            User.promotion,
            func.count(func.distinct(Transaction.id)).label("nb"),
            func.sum(-Contribution.amount).label("spent"),
        )
        .select_from(Contribution)
        .join(Transaction, Contribution.transaction_id == Transaction.id)
        .join(User, Contribution.user_id == User.id)
        .where(
            Contribution.user_id.isnot(None),
            Transaction.type.in_(SALE_TYPES),
            Transaction.cancelled.is_(False),
            match,
        )
        .group_by(User.id, User.name, User.promotion)
        .order_by(func.sum(-Contribution.amount).desc(), User.name)
        .limit(10)
    ).all()
    promotions = db.session.execute(
        select(
            User.promotion,
            func.count(func.distinct(User.id)).label("buyers"),
            func.sum(-Contribution.amount).label("spent"),
        )
        .select_from(Contribution)
        .join(Transaction, Contribution.transaction_id == Transaction.id)
        .join(User, Contribution.user_id == User.id)
        .where(
            Contribution.user_id.isnot(None),
            Transaction.type.in_(SALE_TYPES),
            Transaction.cancelled.is_(False),
            match,
        )
        .group_by(User.promotion)
    ).all()
    promotion_rows = [
        {
            "promotion": row.promotion,
            "buyers": int(row.buyers or 0),
            "spent": int(row.spent or 0),
        }
        for row in promotions
    ]
    promotion_rows.sort(key=lambda row: (row["promotion"] is None, -(row["promotion"] or 0)))
    return {
        "unique": int(unique or 0),
        "top": [
            {
                "name": row.name,
                "promotion": row.promotion,
                "nb": int(row.nb or 0),
                "spent": int(row.spent or 0),
            }
            for row in top
        ],
        "promotions": promotion_rows,
    }


def _operator_label(label):
    if (label or "").startswith(GATEWAY_PREFIX):
        return "Passerelle"
    return label or "—"


def _view_transaction(transaction, event_id, article_ids):
    """Transaction réduite à sa part événement, pour la page et le CSV."""
    if transaction.event_id == event_id:
        lines = list(transaction.lines)
        deposit_amount = max(
            0, int(transaction.total or 0) - sum(line.line_total for line in lines)
        )
    else:
        lines = [line for line in transaction.lines if line.article_id in article_ids]
        deposit_amount = 0
    buyers = []
    for contribution in transaction.contributions:
        if contribution.user_id and contribution.user and contribution.user.name not in buyers:
            buyers.append(contribution.user.name)
    line_total = sum(line.line_total for line in lines)
    return {
        "id": transaction.id,
        "created_at": transaction.created_at,
        "operator_label": transaction.operator_label or "",
        "payment_method": transaction.payment_method,
        "cancelled": bool(transaction.cancelled),
        "lines": lines,
        "buyers": buyers,
        "deposit_glasses": transaction.deposit_glasses if transaction.event_id == event_id else 0,
        "deposit_amount": deposit_amount,
        "line_total": line_total,
        "total": line_total + deposit_amount,
    }


def _matched_transaction_query(event):
    ids = (
        select(Transaction.id)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .where(Transaction.type.in_(SALE_TYPES), _line_scope(event))
        .distinct()
    )
    return (
        select(Transaction)
        .where(Transaction.id.in_(ids))
        .options(
            selectinload(Transaction.lines),
            selectinload(Transaction.contributions).selectinload(Contribution.user),
        )
    )


def transactions_for_export(event, article_ids=None):
    """Toutes les transactions de l'événement, dans l'ordre chronologique."""
    if article_ids is None:
        article_ids = set(
            db.session.scalars(select(Article.id).where(Article.event_id == event.id))
        )
    rows = db.session.scalars(
        _matched_transaction_query(event).order_by(Transaction.created_at, Transaction.id)
    ).all()
    return [_view_transaction(row, event.id, article_ids) for row in rows]


def _transaction_page(event, page, per_page, article_ids):
    base = (
        select(Transaction.id)
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .where(Transaction.type.in_(SALE_TYPES), _line_scope(event))
        .distinct()
    )
    total = db.session.scalar(select(func.count()).select_from(base.subquery())) or 0
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    rows = db.session.scalars(
        _matched_transaction_query(event)
        .order_by(Transaction.created_at.desc(), Transaction.id.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()
    return {
        "rows": [_view_transaction(row, event.id, article_ids) for row in rows],
        "page": page,
        "pages": pages,
        "total": int(total),
        "per_page": per_page,
    }


def build_recap(event, page=1, per_page=50):
    article_ids = set(db.session.scalars(select(Article.id).where(Article.event_id == event.id)))
    all_sales = _sales_rows(event)
    active = [row for row in all_sales if not row["cancelled"]]
    cancelled = [row for row in all_sales if row["cancelled"]]

    revenue = sum(row["lines_total"] for row in active)
    deposits = sum(_deposit(row, event.id) for row in active)
    sales_count = len(active)
    summary = {
        "sales_count": sales_count,
        "sold_items": sum(row["items"] for row in active),
        "revenue": revenue,
        "deposits": deposits,
        "collected": revenue + deposits,
        "average_basket": round(revenue / sales_count) if sales_count else 0,
        "cancelled_count": len(cancelled),
        "cancelled_revenue": sum(row["lines_total"] for row in cancelled),
        "cancelled_deposits": sum(_deposit(row, event.id) for row in cancelled),
    }

    articles = _article_rows(event)
    for article in articles:
        article["liters"] = round(article["qty"] * (article["volume_cl"] or 0) / 100, 2)
        article["share"] = round(article["revenue"] / revenue * 100, 1) if revenue else 0
    articles.sort(key=lambda row: (-row["revenue"], -row["qty"], row["name"].casefold()))

    categories = {}
    for article in articles:
        category = categories.setdefault(
            article["article_type"],
            {
                "type": article["article_type"],
                "label": ARTICLE_TYPES.get(article["article_type"], article["article_type"]),
                "qty": 0,
                "revenue": 0,
                "sales": 0,
            },
        )
        category["qty"] += article["qty"]
        category["revenue"] += article["revenue"]
        category["sales"] += article["sales"]
    category_rows = sorted(categories.values(), key=lambda row: -row["revenue"])

    kegs = {}
    unknown_volume_items = 0
    for article in articles:
        if article["volume_cl"] is None:
            unknown_volume_items += article["qty"]
        if article["keg_id"] is None or not article["volume_cl"]:
            continue
        keg = kegs.setdefault(
            article["keg_id"],
            {
                "name": article["keg_name"] or "Fût",
                "volume_l": article["keg_volume_l"],
                "liters": 0.0,
            },
        )
        keg["liters"] += article["liters"]
    for keg in kegs.values():
        keg["liters"] = round(keg["liters"], 2)
        keg["equivalent"] = (
            round(keg["liters"] / keg["volume_l"], 2) if keg["volume_l"] > 0 else None
        )
    keg_rows = sorted(kegs.values(), key=lambda row: -row["liters"])
    keg_liters = round(sum(keg["liters"] for keg in keg_rows), 2)
    total_liters = round(sum(article["liters"] for article in articles), 2)
    volumes = {
        "total_liters": total_liters,
        "keg_liters": keg_liters,
        "other_liters": round(total_liters - keg_liters, 2),
        "unknown_items": unknown_volume_items,
        "kegs": keg_rows,
        "total_equivalent": round(
            sum(keg["equivalent"] or 0 for keg in keg_rows),
            2,
        ),
    }

    operators = {}
    for row in active:
        label = _operator_label(row["operator_label"])
        operator = operators.setdefault(
            label,
            {
                "label": label,
                "gateway": label == "Passerelle",
                "sales": 0,
                "sold_items": 0,
                "revenue": 0,
                "deposits": 0,
                "collected": 0,
            },
        )
        operator["sales"] += 1
        operator["sold_items"] += row["items"]
        operator["revenue"] += row["lines_total"]
        operator["deposits"] += _deposit(row, event.id)
    for operator in operators.values():
        operator["collected"] = operator["revenue"] + operator["deposits"]
    operator_rows = sorted(
        operators.values(), key=lambda row: (-row["collected"], row["label"].casefold())
    )

    hours = [
        {"hour": hour, "label": f"{hour:02d} h – {(hour + 1) % 24:02d} h", "sales": 0, "revenue": 0}
        for hour in range(24)
    ]
    for row in active:
        slot = hours[to_paris(row["created_at"]).hour]
        slot["sales"] += 1
        slot["revenue"] += row["lines_total"]
    max_sales = max((slot["sales"] for slot in hours), default=0)
    for slot in hours:
        slot["width"] = round(slot["sales"] / max_sales * 100, 1) if max_sales else 0
    peak = max(hours, key=lambda slot: (slot["sales"], slot["revenue"])) if active else None
    if peak is not None and peak["sales"] == 0:
        peak = None

    buyers = _buyer_stats(event)
    transparent_names = plain_names(
        {
            row["operator_label"]
            for row in all_sales
            if row["operator_label"] and not row["operator_label"].startswith(GATEWAY_PREFIX)
        },
        255,
    )

    def operator_display(label):
        if (label or "").startswith(GATEWAY_PREFIX):
            return "Passerelle"
        return transparent_names.get(label, label or "—")

    transactions = _transaction_page(event, page, per_page, article_ids)
    for row in transactions["rows"]:
        row["operator"] = operator_display(row["operator_label"])
    for operator in operator_rows:
        operator["display"] = operator_display(operator["label"])

    return {
        "summary": summary,
        "articles": articles,
        "categories": category_rows,
        "volumes": volumes,
        "operators": operator_rows,
        "hours": hours,
        "peak": peak,
        "buyers": buyers,
        "transactions": transactions,
    }


def export_rows(event):
    """En-tête et lignes du CSV détaillé (une ligne par ligne de vente)."""
    header = [
        "Date",
        "N° transaction",
        "Canal",
        "Opérateur",
        "Paiement",
        "Statut",
        "Acheteurs",
        "Article",
        "Catégorie",
        "Quantité",
        "Prix unitaire (EUR)",
        "Total ligne (EUR)",
        "Total part événement (EUR)",
        "Consignes (verres)",
        "Montant consignes (EUR)",
    ]
    rows = []
    operators = {}
    for transaction in transactions_for_export(event):
        label = _operator_label(transaction["operator_label"])
        if label not in operators:
            operators[label] = (
                "Passerelle"
                if label == "Passerelle"
                else plain_names({label}, 255).get(label, label)
            )
        if transaction["payment_method"] in PAYMENT_METHODS:
            payment = PAYMENT_METHODS[transaction["payment_method"]]
        elif transaction["payment_method"]:
            payment = transaction["payment_method"]
        else:
            payment = "Portefeuille étudiant"
        common = [
            to_paris(transaction["created_at"]).strftime("%Y-%m-%d %H:%M"),
            str(transaction["id"]),
            "Passerelle" if transaction["operator_label"].startswith(GATEWAY_PREFIX) else "Caisse",
            operators[label],
            payment,
            "Annulée" if transaction["cancelled"] else "Validée",
            ", ".join(transaction["buyers"]),
        ]
        if not transaction["lines"]:
            rows.append(
                [
                    *common,
                    "",
                    "",
                    "",
                    "",
                    "",
                    f"{transaction['total'] / 100:.2f}",
                    str(transaction["deposit_glasses"]),
                    f"{transaction['deposit_amount'] / 100:.2f}",
                ]
            )
            continue
        for line in transaction["lines"]:
            rows.append(
                [
                    *common,
                    line.article_name,
                    ARTICLE_TYPES.get(line.article_type, line.article_type),
                    str(line.quantity),
                    f"{line.unit_price / 100:.2f}",
                    f"{line.line_total / 100:.2f}",
                    f"{transaction['total'] / 100:.2f}",
                    str(transaction["deposit_glasses"]),
                    f"{transaction['deposit_amount'] / 100:.2f}",
                ]
            )
    return header, rows
