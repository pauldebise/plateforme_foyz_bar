import time
from collections import defaultdict
from datetime import date, datetime, time as dt_time, timedelta

from sqlalchemy import Float, case, cast, exists, func, literal, select

from app.extensions import db
from app.models import Contribution, Event, Transaction, TransactionLine, User
from app.services.transactions import history_cutoff, ranking_generation
from app.utils import (
    ARTICLE_TYPES,
    CAMPUSSES,
    PARIS_TZ,
    fold_text,
    paris_to_utc,
    to_paris,
    utcnow,
)

# Une période personnalisée trop large rendrait la page inutilisable (la série
# journalière est découpée en Python pour suivre le fuseau de Paris). Trois ans
# couvrent largement l'historique conservé.
MAX_STATS_DAYS = 1100

PERIOD_PRESETS = {
    "7j": "7 derniers jours",
    "30j": "30 derniers jours",
    "mois": "Ce mois-ci",
    "mois-1": "Mois dernier",
    "6mois": "6 derniers mois",
    "12mois": "12 derniers mois",
    "annee": "Cette année",
    "evenement": "Dernier événement",
    "custom": "Personnalisé",
}

MONTHS_FR = (
    "janv.",
    "févr.",
    "mars",
    "avr.",
    "mai",
    "juin",
    "juil.",
    "août",
    "sept.",
    "oct.",
    "nov.",
    "déc.",
)
MONTHS_FR_LONG = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)
WEEKDAYS_FR = ("lun.", "mar.", "mer.", "jeu.", "ven.", "sam.", "dim.")


def _parse_date(value, end_of_day=False):
    if not value:
        return None
    try:
        d = datetime.strptime(value, "%Y-%m-%d")
        return d + timedelta(days=1, microseconds=-1) if end_of_day else d
    except ValueError:
        return None


def _parse_day(value):
    try:
        return date.fromisoformat(str(value or "").strip())
    except (TypeError, ValueError):
        return None


def _int(value):
    """Entier à partir d'un agrégat SQL (Decimal, float ou None)."""
    return int(value or 0)


def promotion_int(value):
    """Année de promotion valide (4 chiffres en pratique), sinon None.

    La borne de longueur évite qu'une saisie démesurée ne fasse échouer la
    conversion entière et ne produise une erreur serveur.
    """
    text = str(value or "").strip()
    if not text.lstrip("-").isdigit() or len(text) > 6:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _month_start(day):
    return day.replace(day=1)


def _shift_month(day, months):
    total = day.year * 12 + (day.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def _month_end(day):
    return _shift_month(day, 1) - timedelta(days=1)


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
        promotion_value = promotion_int(promotion)
        if promotion_value is None:
            return db.false()
        cond = cond.where(User.promotion == promotion_value)
    if team_only == "team":
        cond = cond.where(User.team_status.is_not(None))
    elif team_only == "non_team":
        cond = cond.where(User.team_status.is_(None))
    return exists(cond)


def _apply_transaction_filters(stmt, filters, cutoff, types):
    """Filtres communs aux agrégats de ventes (type, campus, dates, portée
    utilisateur, soirées)."""
    stmt = stmt.where(
        Transaction.type.in_(types),
        Transaction.cancelled.is_(False),
        Transaction.created_at >= cutoff,
    )
    campus = filters.get("campus")
    if campus in CAMPUSSES:
        stmt = stmt.where(Transaction.campus == campus)
    date_from = _parse_date(filters.get("date_from"))
    date_to = _parse_date(filters.get("date_to"), end_of_day=True)
    if date_from:
        stmt = stmt.where(Transaction.created_at >= date_from)
    if date_to:
        stmt = stmt.where(Transaction.created_at <= date_to)
    event_scope = filters.get("events", "")
    if event_scope == "events":
        stmt = stmt.where(Transaction.event_id.isnot(None))
    elif event_scope == "hors_events":
        stmt = stmt.where(Transaction.event_id.is_(None))
    scope = _match_user_scope(filters)
    if scope is not None:
        stmt = stmt.where(scope)
    return stmt


def _restrict_to_category(stmt, category):
    """Restreint une requête portant sur des transactions aux transactions
    contenant au moins une ligne de la catégorie demandée."""
    if not category:
        return stmt
    return stmt.where(
        Transaction.id.in_(
            select(TransactionLine.transaction_id).where(TransactionLine.article_type == category)
        )
    )


def category_totals(filters=None):
    """Totaux par catégorie et total général, calculés par la base."""
    filters = filters or {}
    cutoff = history_cutoff()
    category = filters.get("category")

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
    return {
        "grand_total": grand_total,
        "by_category": {k: v for k, v in sorted(totals.items(), key=lambda kv: -kv[1]["revenue"])},
    }


def _series_raw(filters, with_counts=False):
    """Série brute par jour de Paris : recettes, et si demandé articles,
    transactions et croisement mensuel par catégorie.

    Le découpage en jours de Paris dépend de l'heure d'été/hiver, on ne peut
    pas le faire en SQL de façon portable. On rapatrie donc uniquement
    (date, montant, quantité, catégorie), sans objet ORM ni chargement des
    lignes/participants, en flux pour borner la mémoire.
    """
    filters = filters or {}
    cutoff = history_cutoff()
    category = filters.get("category")

    columns = [Transaction.created_at, TransactionLine.line_total]
    if with_counts:
        columns.extend([TransactionLine.quantity, TransactionLine.article_type])
    series_stmt = (
        select(*columns)
        .select_from(TransactionLine)
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
    )
    series_stmt = _apply_transaction_filters(series_stmt, filters, cutoff, ("achat", "direct"))
    if category:
        series_stmt = series_stmt.where(TransactionLine.article_type == category)

    revenue = defaultdict(int)
    quantities = defaultdict(int)
    monthly = defaultdict(lambda: defaultdict(lambda: {"qty": 0, "revenue": 0}))
    for row in db.session.execute(series_stmt).yield_per(1000):
        created_at, line_total = row[0], row[1]
        day = to_paris(created_at).date()
        key = day.isoformat()
        revenue[key] += line_total
        if with_counts:
            quantity, article_type = row[2], row[3]
            quantities[key] += _int(quantity)
            month = day.strftime("%Y-%m")
            monthly[month][article_type]["qty"] += _int(quantity)
            monthly[month][article_type]["revenue"] += line_total

    transactions = defaultdict(int)
    if with_counts:
        tx_stmt = select(Transaction.created_at).select_from(Transaction)
        tx_stmt = _apply_transaction_filters(tx_stmt, filters, cutoff, ("achat", "direct"))
        tx_stmt = _restrict_to_category(tx_stmt, category)
        for (created_at,) in db.session.execute(tx_stmt):
            transactions[to_paris(created_at).date().isoformat()] += 1

    return dict(revenue), dict(quantities), dict(transactions), monthly


def _monthly_payload(monthly, start, end):
    """Série mensuelle complète (mois sans vente inclus) par catégorie."""
    labels = []
    cursor = _month_start(start)
    while cursor <= end:
        labels.append(cursor.strftime("%Y-%m"))
        cursor = _shift_month(cursor, 1)

    totals_by_type = defaultdict(lambda: {"qty": 0, "revenue": 0})
    for month in labels:
        for article_type, values in monthly.get(month, {}).items():
            totals_by_type[article_type]["qty"] += values["qty"]
            totals_by_type[article_type]["revenue"] += values["revenue"]

    ordered = sorted(totals_by_type, key=lambda key: -totals_by_type[key]["revenue"])
    series = []
    for article_type in ordered:
        series.append(
            {
                "type": article_type,
                "label": ARTICLE_TYPES.get(article_type, article_type),
                "qty": [
                    monthly.get(month, {}).get(article_type, {}).get("qty", 0) for month in labels
                ],
                "revenue": [
                    monthly.get(month, {}).get(article_type, {}).get("revenue", 0)
                    for month in labels
                ],
            }
        )
    return {"labels": labels, "series": series}


def daily_series(filters, start, end):
    """Série journalière complète entre deux dates, jours sans vente à zéro."""
    revenue, quantities, transactions, monthly = _series_raw(filters, with_counts=True)
    days = (end - start).days + 1
    dates = [(start + timedelta(days=offset)).isoformat() for offset in range(days)]
    return {
        "dates": dates,
        "revenue": [revenue.get(day, 0) for day in dates],
        "qty": [quantities.get(day, 0) for day in dates],
        "transactions": [transactions.get(day, 0) for day in dates],
        "monthly": _monthly_payload(monthly, start, end),
    }


def granularity_for(start, end):
    """Pas de lecture de la tendance : jour, semaine ou mois."""
    days = (end - start).days + 1
    if days <= 62:
        return "jour"
    if days <= 240:
        return "semaine"
    return "mois"


def _bucket_label(kind, display_start, display_end):
    """Libellé court d'un point (axe) et libellé long (infobulle)."""
    if kind == "jour":
        short = f"{display_start.day} {MONTHS_FR[display_start.month - 1]}"
        long = (
            f"{WEEKDAYS_FR[display_start.weekday()]} {display_start.day} "
            f"{MONTHS_FR_LONG[display_start.month - 1]} {display_start.year}"
        )
        best = f"{WEEKDAYS_FR[display_start.weekday()]} {display_start.day} {MONTHS_FR[display_start.month - 1]}"
        return short, long, best

    if kind == "semaine":
        short = f"{display_start.day} {MONTHS_FR[display_start.month - 1]}"
        if display_start.year != display_end.year:
            long = (
                f"Semaine du {display_start.day} {MONTHS_FR_LONG[display_start.month - 1]} "
                f"{display_start.year} au {display_end.day} "
                f"{MONTHS_FR_LONG[display_end.month - 1]} {display_end.year}"
            )
        elif display_start.month == display_end.month:
            long = (
                f"Semaine du {display_start.day} au {display_end.day} "
                f"{MONTHS_FR_LONG[display_end.month - 1]} {display_end.year}"
            )
        else:
            long = (
                f"Semaine du {display_start.day} {MONTHS_FR_LONG[display_start.month - 1]} "
                f"au {display_end.day} {MONTHS_FR_LONG[display_end.month - 1]} {display_end.year}"
            )
        best = f"semaine du {display_start.day} {MONTHS_FR[display_start.month - 1]}"
        return short, long, best

    short = f"{MONTHS_FR[display_start.month - 1]} {display_start.year}"
    long = f"{MONTHS_FR_LONG[display_start.month - 1]} {display_start.year}"
    return short, long, long


def bucket_series(series, start, end, kind="jour"):
    """Regroupe la série journalière par jour, semaine (lundi) ou mois."""
    buckets = {}
    for index, iso in enumerate(series["dates"]):
        day = date.fromisoformat(iso)
        if kind == "semaine":
            key = day - timedelta(days=day.weekday())
        elif kind == "mois":
            key = day.replace(day=1)
        else:
            key = day
        bucket = buckets.setdefault(key, {"revenue": 0, "qty": 0, "transactions": 0})
        bucket["revenue"] += series["revenue"][index]
        bucket["qty"] += series["qty"][index]
        bucket["transactions"] += series["transactions"][index]

    keys = sorted(buckets)
    result = {
        "granularity": kind,
        "dates": [],
        "starts": [],
        "ends": [],
        "labels": [],
        "tooltips": [],
        "best_labels": [],
        "revenue": [],
        "qty": [],
        "transactions": [],
    }
    for key in keys:
        if kind == "semaine":
            natural_end = key + timedelta(days=6)
        elif kind == "mois":
            natural_end = _month_end(key)
        else:
            natural_end = key
        display_start = max(key, start)
        display_end = min(natural_end, end)
        short, long, best = _bucket_label(kind, display_start, display_end)
        bucket = buckets[key]
        result["dates"].append(key.isoformat())
        result["starts"].append(display_start.isoformat())
        result["ends"].append(display_end.isoformat())
        result["labels"].append(short)
        result["tooltips"].append(long)
        result["best_labels"].append(best)
        result["revenue"].append(bucket["revenue"])
        result["qty"].append(bucket["qty"])
        result["transactions"].append(bucket["transactions"])
    return result


def _count_transactions(filters):
    cutoff = history_cutoff()
    stmt = select(func.count(func.distinct(Transaction.id))).select_from(Transaction)
    stmt = _apply_transaction_filters(stmt, filters, cutoff, ("achat", "direct"))
    stmt = _restrict_to_category(stmt, filters.get("category"))
    return _int(db.session.execute(stmt).scalar_one())


def _count_students(filters):
    """Acheteurs uniques (participants à un achat réglé sur compte)."""
    cutoff = history_cutoff()
    stmt = (
        select(func.count(func.distinct(User.id)))
        .select_from(Transaction)
        .join(Contribution, Contribution.transaction_id == Transaction.id)
        .join(User, Contribution.user_id == User.id)
    )
    stmt = _apply_transaction_filters(stmt, filters, cutoff, ("achat", "direct"))
    stmt = _restrict_to_category(stmt, filters.get("category"))
    promotion = filters.get("promotion")
    if promotion:
        promotion_value = promotion_int(promotion)
        if promotion_value is None:
            return 0
        stmt = stmt.where(User.promotion == promotion_value)
    team_only = filters.get("team_only", "")
    if team_only == "team":
        stmt = stmt.where(User.team_status.in_(["mandat", "ancien"]))
    elif team_only == "non_team":
        stmt = stmt.where(
            db.or_(User.team_status.is_(None), User.team_status.notin_(["mandat", "ancien"]))
        )
    return _int(db.session.execute(stmt).scalar_one())


def period_totals(filters=None):
    """Indicateurs de la période : recettes, volume, transactions, étudiants."""
    filters = filters or {}
    totals = category_totals(filters)
    return {
        "revenue": totals["grand_total"]["revenue"],
        "qty": totals["grand_total"]["qty"],
        "transactions": _count_transactions(filters),
        "students": _count_students(filters),
        "by_category": totals["by_category"],
    }


def percent_delta(current, previous):
    """Écart relatif affichable, ou « — » quand la période précédente est vide."""
    if not previous:
        return {"tone": "none", "label": "—", "detail": "pas de comparaison"}
    value = (current - previous) * 100 / previous
    if abs(value) < 0.05:
        return {"tone": "flat", "label": "0 %", "detail": "stable"}
    tone = "up" if value > 0 else "down"
    sign = "+" if value > 0 else ""
    label = f"{sign}{value:.1f} %".replace(".", ",")
    return {"tone": tone, "label": label, "detail": "vs période préc."}


def previous_period(start, end):
    """Période précédente de même durée, juste avant `start`."""
    days = (end - start).days + 1
    previous_end = start - timedelta(days=1)
    return previous_end - timedelta(days=days - 1), previous_end


def _event_label(event, start=None, end=None):
    event_start = to_paris(event.starts_at).date()
    event_end = to_paris(event.ends_at).date()
    if start is not None:
        event_start = max(event_start, start)
    if end is not None:
        event_end = min(event_end, end)
    if event_start == event_end:
        when = f"{event_start.day} {MONTHS_FR[event_start.month - 1]}"
    elif event_start.year == event_end.year:
        when = (
            f"{event_start.day} {MONTHS_FR[event_start.month - 1]} – "
            f"{event_end.day} {MONTHS_FR[event_end.month - 1]}"
        )
    else:
        when = (
            f"{event_start.day} {MONTHS_FR[event_start.month - 1]} {event_start.year} – "
            f"{event_end.day} {MONTHS_FR[event_end.month - 1]} {event_end.year}"
        )
    return f"{event.name} ({when})"


def latest_event(campus=""):
    stmt = select(Event).where(Event.starts_at <= utcnow())
    if campus in CAMPUSSES:
        stmt = stmt.where(Event.campus == campus)
    return db.session.scalars(stmt.order_by(Event.starts_at.desc()).limit(1)).first()


def events_in_period(start, end, campus=""):
    """Soirées qui chevauchent la période, avec leurs dates de Paris."""
    start_utc = paris_to_utc(datetime.combine(start, dt_time.min))
    end_utc = paris_to_utc(datetime.combine(end + timedelta(days=1), dt_time.min))
    stmt = select(Event).where(Event.starts_at < end_utc, Event.ends_at >= start_utc)
    if campus in CAMPUSSES:
        stmt = stmt.where(Event.campus == campus)
    result = []
    for event in db.session.scalars(stmt.order_by(Event.starts_at)):
        event_start = max(to_paris(event.starts_at).date(), start)
        event_end = min(to_paris(event.ends_at).date(), end)
        if event_start > event_end:
            continue
        dates = [
            (event_start + timedelta(days=offset)).isoformat()
            for offset in range((event_end - event_start).days + 1)
        ]
        result.append(
            {
                "name": event.name,
                "label": _event_label(event),
                "dates": dates,
            }
        )
    return result


def resolve_period(preset="30j", date_from="", date_to="", campus="", today=None):
    """Fenêtre effective de la page statistiques.

    Renvoie un dictionnaire `start`, `end`, `preset`, `note` et `event`.
    Les périodes sont annuelles ou glissantes ; une date de fin future est
    ramenée à aujourd'hui.
    """
    today = today or datetime.now(PARIS_TZ).date()
    preset = (preset or "30j").strip()
    if preset not in PERIOD_PRESETS:
        preset = "30j"
    note = ""
    event = None

    if preset == "custom":
        start = _parse_day(date_from)
        end = _parse_day(date_to)
        if start is None and end is None:
            preset = "30j"
            note = "Dates invalides : les 30 derniers jours sont affichés."
        elif start is None:
            start = end - timedelta(days=29)
            note = "Date de début invalide : 30 jours avant la fin sont affichés."
        elif end is None:
            end = today
            note = "Date de fin invalide : la période se termine aujourd'hui."
        if start is not None and end is not None:
            if start > end:
                start, end = end, start
                note = "Les dates ont été inversées."
            if end > today:
                end = today
                note = "La période ne peut pas dépasser aujourd'hui."
            if start > end:
                start = end
            if (end - start).days + 1 > MAX_STATS_DAYS:
                start = end - timedelta(days=MAX_STATS_DAYS - 1)
                note = "Période limitée aux trois dernières années."

    if preset == "7j":
        start, end = today - timedelta(days=6), today
    elif preset == "30j":
        start, end = today - timedelta(days=29), today
    elif preset == "mois":
        start, end = _month_start(today), today
    elif preset == "mois-1":
        previous_month = _shift_month(today, -1)
        start, end = previous_month, _month_end(previous_month)
    elif preset == "6mois":
        start, end = _shift_month(today, -5), today
    elif preset == "12mois":
        start, end = _shift_month(today, -11), today
    elif preset == "annee":
        start, end = date(today.year, 1, 1), today
    elif preset == "evenement":
        event = latest_event(campus)
        if event is None:
            preset = "30j"
            start, end = today - timedelta(days=29), today
            note = "Aucun événement enregistré : les 30 derniers jours sont affichés."
        else:
            start = to_paris(event.starts_at).date()
            end = min(to_paris(event.ends_at).date(), today)
            if start > end:
                start = end

    if (end - start).days + 1 > MAX_STATS_DAYS:
        start = end - timedelta(days=MAX_STATS_DAYS - 1)
        note = "Période limitée aux trois dernières années."

    return {
        "start": start,
        "end": end,
        "preset": preset,
        "note": note,
        "event": _event_label(event) if event is not None else "",
    }


def format_period(start, end):
    if start == end:
        return f"{start.day} {MONTHS_FR[start.month - 1]} {start.year}"
    if start.year == end.year and start.month == end.month:
        return f"{start.day}–{end.day} {MONTHS_FR[start.month - 1]} {start.year}"
    if start.year == end.year:
        return (
            f"{start.day} {MONTHS_FR[start.month - 1]} – "
            f"{end.day} {MONTHS_FR[end.month - 1]} {end.year}"
        )
    return (
        f"{start.day} {MONTHS_FR[start.month - 1]} {start.year} – "
        f"{end.day} {MONTHS_FR[end.month - 1]} {end.year}"
    )


def format_day(day):
    return f"{WEEKDAYS_FR[day.weekday()]} {day.day} {MONTHS_FR[day.month - 1]}"


def sales_stats(filters=None):
    """Format historique de /api/stats : totaux, catégories et série."""
    totals = category_totals(filters)
    revenue, _quantities, _transactions, _monthly = _series_raw(filters)
    return {
        "grand_total": totals["grand_total"],
        "by_category": totals["by_category"],
        "series": dict(sorted(revenue.items())),
    }


def _normalize_page(page):
    try:
        return max(1, int(page))
    except (TypeError, ValueError):
        return 1


def articles_stats(filters=None, search="", sort="volume", page=1, per_page=0):
    """Classement paginé des articles vendus sur la période filtrée."""
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
        needle = fold_text(search)
        articles = [
            article
            for article in articles
            if needle in fold_text(article["name"])
            or needle in fold_text(article["type"])
            or needle in fold_text(ARTICLE_TYPES.get(article["type"], ""))
        ]

    if sort == "revenu":
        articles.sort(key=lambda article: (-article["revenue"], -article["qty"], article["name"]))
    elif sort == "nom":
        articles.sort(key=lambda article: (fold_text(article["name"]), -article["revenue"]))
    else:
        sort = "volume"
        articles.sort(key=lambda article: (-article["qty"], -article["revenue"], article["name"]))

    total = len(articles)
    max_revenue = max((article["revenue"] for article in articles), default=0)
    result = {
        "rows": articles,
        "total": total,
        "page": 1,
        "pages": 1,
        "max_revenue": max_revenue,
    }
    if not per_page:
        return result

    try:
        per_page = max(1, int(per_page))
    except (TypeError, ValueError):
        per_page = 25
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(_normalize_page(page), pages)
    start = (page - 1) * per_page
    result.update(
        rows=articles[start : start + per_page],
        page=page,
        pages=pages,
    )
    return result


def top_articles_stats(filters=None, search=""):
    """Compatibilité : liste complète triée par volume décroissant."""
    return articles_stats(filters, search=search, per_page=0)["rows"]


def students_stats(filters=None, search="", sort="depense", page=1, per_page=0):
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
    if campus in CAMPUSSES:
        stmt = stmt.where(Transaction.campus == campus)
    date_from = _parse_date(filters.get("date_from"))
    date_to = _parse_date(filters.get("date_to"), end_of_day=True)
    if date_from:
        stmt = stmt.where(Transaction.created_at >= date_from)
    if date_to:
        stmt = stmt.where(Transaction.created_at <= date_to)
    event_scope = filters.get("events", "")
    if event_scope == "events":
        stmt = stmt.where(Transaction.event_id.isnot(None))
    elif event_scope == "hors_events":
        stmt = stmt.where(Transaction.event_id.is_(None))
    if promotion:
        promotion_value = promotion_int(promotion)
        if promotion_value is None:
            stmt = stmt.where(db.false())
        else:
            stmt = stmt.where(User.promotion == promotion_value)
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
    if search:
        # Chaque mot doit apparaître, dans n'importe quel ordre : « prénom nom »
        # retrouve un compte enregistré « nom prénom ».
        needles = [token for token in fold_text(search).split() if token]
        result = [
            student
            for student in result
            if all(needle in fold_text(student["name"]) for needle in needles)
        ]

    if sort == "nom":
        result.sort(key=lambda student: (fold_text(student["name"]), -student["spent"]))
    elif sort == "achats":
        result.sort(
            key=lambda student: (-student["nb"], -student["spent"], fold_text(student["name"]))
        )
    elif sort == "articles":
        result.sort(
            key=lambda student: (
                -student["articles"],
                -student["spent"],
                fold_text(student["name"]),
            )
        )
    else:
        sort = "depense"
        result.sort(key=lambda student: (-student["spent"], fold_text(student["name"])))

    total_spent = sum(student["spent"] for student in result)
    max_spent = max((student["spent"] for student in result), default=0)
    for student in result:
        student["average"] = round(student["spent"] / student["nb"]) if student["nb"] else 0
        student["share"] = round(student["spent"] * 100 / total_spent) if total_spent else 0
        student["bar"] = round(student["spent"] * 100 / max_spent) if max_spent else 0
        student["initials"] = _initials(student["name"])

    total = len(result)
    team_total = sum(1 for student in result if student["is_team"])
    average_spent = round(total_spent / total) if total else 0
    top = result[:10]
    if not per_page:
        return {
            "rows": result,
            "top": top,
            "total": total,
            "page": 1,
            "pages": 1,
            "max_spent": max_spent,
            "total_spent": total_spent,
            "average_spent": average_spent,
            "team_total": team_total,
            "sort": sort,
        }
    try:
        per_page = max(1, int(per_page))
    except (TypeError, ValueError):
        per_page = 25
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(_normalize_page(page), pages)
    start = (page - 1) * per_page
    return {
        "rows": result[start : start + per_page],
        "top": top,
        "total": total,
        "page": page,
        "pages": pages,
        "max_spent": max_spent,
        "total_spent": total_spent,
        "average_spent": average_spent,
        "team_total": team_total,
        "sort": sort,
    }


def _initials(name):
    """Initiales lisibles pour la pastille du tableau des étudiants."""
    words = [word for word in str(name or "").replace("(", " ").split() if word[:1].isalpha()]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[1][0]).upper()


# Classement du catalogue : recalculé au plus une fois par génération
# d'encaissement et par fenêtre, avec un TTL qui couvre les autres workers.
_TOP_ARTICLES_TTL_SECONDS = 30
_top_articles_cache = {}


TOP_ARTICLES_DAYS = 45


def top_article_sales(campus=None, limit=None, days=TOP_ARTICLES_DAYS):
    """Classement des articles par popularité récente, comme le tri des
    étudiants : liste de (article_id, quantité vendue) sur les `days` derniers
    jours, quantité décroissante puis date de la dernière vente décroissante.
    Les articles sans vente récente n'apparaissent pas. Résultat mis en cache
    (génération d'encaissement + TTL) : partagé par la caisse, la passerelle et
    l'onglet Articles de l'administration."""
    generation = ranking_generation()
    key = (campus, limit, days)
    now = time.monotonic()
    cached = _top_articles_cache.get(key)
    if cached is not None and cached[0] > now and cached[1] == generation:
        return cached[2]

    since = utcnow() - timedelta(days=days)
    # Transactions récentes sélectionnées à part : le planificateur s'appuie sur
    # l'index `transaction_lines.transaction_id` au lieu de balayer toute la
    # table des lignes (des centaines de milliers, dont la majorité est hors
    # fenêtre).
    recent = select(Transaction.id).where(
        Transaction.type.in_(["achat", "direct"]),
        Transaction.cancelled.is_(False),
        Transaction.created_at >= since,
    )
    if campus in ("brest", "paris"):
        recent = recent.where(Transaction.campus == campus)
    stmt = (
        select(TransactionLine.article_id, func.sum(TransactionLine.quantity).label("qty"))
        .join(Transaction, TransactionLine.transaction_id == Transaction.id)
        .where(
            TransactionLine.article_id.isnot(None),
            TransactionLine.transaction_id.in_(recent),
        )
        .group_by(TransactionLine.article_id)
        .order_by(
            func.sum(TransactionLine.quantity).desc(),
            func.max(Transaction.created_at).desc(),
        )
    )
    if limit:
        stmt = stmt.limit(limit)
    ranking = [(aid, _int(qty)) for aid, qty in db.session.execute(stmt)]
    _top_articles_cache[key] = (now + _TOP_ARTICLES_TTL_SECONDS, generation, ranking)
    return ranking


def top_article_ids(campus=None, limit=None, days=TOP_ARTICLES_DAYS):
    """Identifiants des articles populaires, du plus au moins vendu."""
    return [aid for aid, _qty in top_article_sales(campus, limit, days)]
