import csv
import io
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func, or_, select

from app.extensions import db
from app.models import Article, Contribution, Note, Transaction, TransactionLine, User
from app.routes.auth import login  # noqa: F401
from app.services import transactions as T
from app.services import adminpw as AP
from app.services.events import running_events
from app.services.settings import int_setting
from app.services.names import plain_names, public_note_authors
from app.services import stats as ST
from app.services.treasury import treasury
from app.utils import (
    ARTICLE_TYPES,
    CAMPUSSES,
    PARIS_TZ,
    PAYMENT_METHODS,
    TRANSACTION_TYPES,
    clamp_text,
    is_safe_target,
    login_required,
    parse_amount,
    utcnow,
)

bp = Blueprint("team", __name__)

# Filtre supplémentaire de l'historique : ventes contenant au moins un article
# d'événement. Cette valeur n'existe pas dans `transactions.type` ; elle est
# traitée à part dans la route.
EVENT_SALES_FILTER = "ventes_evenement"
HISTORY_TYPE_FILTERS = {
    **TRANSACTION_TYPES,
    EVENT_SALES_FILTER: "Ventes d'évènement",
}

STATS_VIEWS = {
    "vue": ("Vue d'ensemble", "bi-speedometer2"),
    "articles": ("Articles", "bi-trophy"),
    "etudiants": ("Étudiants", "bi-people"),
}
STATS_TEAM_FILTERS = {
    "": "Tous",
    "team": "Équipe Foy'z/Bar",
    "non_team": "Non-équipe",
}
STATS_EVENT_FILTERS = {
    "": "Toutes",
    "hors_events": "Hors soirée",
    "events": "Soirées uniquement",
}
STATS_ARTICLE_SORTS = ("volume", "revenu", "nom")
STATS_STUDENT_SORTS = ("depense", "achats", "articles", "nom")


@bp.before_request
def require_team_session():
    if not g.get("current_user"):
        return redirect(url_for("auth.login", next=request.path))
    if not session.get("campus"):
        session.clear()
        return redirect(url_for("auth.login", next=request.path))
    return None


def operator():
    return g.current_user.display_name


def campus():
    return session["campus"]


def own_campus():
    """Campus d'appartenance du membre : seul campus où il peut écrire.
    Le compte admin global écrit sur le campus de sa connexion."""
    u = g.current_user
    if u.is_super_admin:
        return campus()
    if u.team_campus in CAMPUSSES:
        return u.team_campus
    return campus()


def can_write():
    return campus() == own_campus()


def require_write():
    """Garde d'écriture : True (et un flash posé) si l'opération est refusée."""
    if can_write():
        return False
    flash(
        f"Campus {CAMPUSSES[campus()]} consulté en lecture seule : les opérations "
        f"sont réservées au campus {CAMPUSSES[own_campus()]}.",
        "danger",
    )
    return True


def require_mandat():
    """Garde : la trésorerie est réservée aux mandats et à l'admin global."""
    u = g.current_user
    if u.team_status != "mandat" and not u.is_super_admin:
        abort(403)


def _payment_catalog(campus_name):
    """Catalogue de la caisse : articles standards et articles des événements
    en cours du campus (même fenêtre que la passerelle)."""
    events = running_events(campus_name)
    event_ids = {event.id for event in events}
    event_names = {event.id: event.name for event in events}
    conditions = [Article.active.is_(True), Article.campus == campus_name]
    if event_ids:
        conditions.append(or_(Article.event_id.is_(None), Article.event_id.in_(event_ids)))
    else:
        conditions.append(Article.event_id.is_(None))
    articles = db.session.scalars(
        select(Article).where(*conditions).order_by(Article.is_tap.desc(), Article.name)
    ).all()
    # un article sans prix sur ce campus (ni standard ni équipe) est exclu de
    # l'encaissement ; il reste listé dans l'onglet admin Articles
    articles = [
        a
        for a in articles
        if a.is_tap or a.price_for(campus_name) or a.price_for(campus_name, team=True)
    ]
    data = []
    rank_of = {aid: i for i, aid in enumerate(ST.top_article_ids(campus_name))}
    for a in articles:
        item = {
            "id": a.id,
            "name": a.name,
            "type": a.article_type,
            "volume": a.volume_cl,
            "alcohol": a.is_alcohol,
            "tap": a.is_tap,
            "tap_number": a.tap_number,
            "std": a.price_for(campus_name, False),
            "team": a.price_for(campus_name, True),
            "event": a.event_id is not None,
        }
        if a.event_id is not None:
            item["event_id"] = a.event_id
            item["event_name"] = event_names.get(a.event_id, "")
        rank = rank_of.get(a.id)
        if rank is not None:
            item["rank"] = rank
        data.append(item)
    return data


@bp.route("/paiement")
@login_required
def payment():
    return render_template(
        "team/payment.html",
        catalog=_payment_catalog(campus()),
        read_only=not can_write(),
    )


@bp.route("/paiement/catalogue")
@login_required
def payment_catalogue():
    """Rafraîchissement périodique du catalogue de la caisse (8 s)."""
    return jsonify(ok=True, catalog=_payment_catalog(campus()))


@bp.route("/rechargement", methods=["GET", "POST"])
@login_required
def rechargement():
    if request.method == "POST":
        if require_write():
            return redirect(url_for("team.rechargement"))
        try:
            user = _get_user_or_fail(request.form.get("user_id"))
            amount = parse_amount(request.form.get("amount", ""))
            t = T.create_reload(
                operator_label=operator(),
                campus=campus(),
                user=user,
                amount_cents=amount,
                payment_method=request.form.get("method", ""),
            )
            flash(
                f"Rechargement de {t.total / 100:.2f} € pour {user.display_name} enregistré.",
                "success",
            )
            return redirect(url_for("team.rechargement"))
        except (T.OperationError, ValueError) as e:
            flash(getattr(e, "message", "Montant invalide."), "danger")
    return render_template("team/operation.html", op="rechargement", read_only=not can_write())


@bp.route("/retrait", methods=["GET", "POST"])
@login_required
def retrait():
    if request.method == "POST":
        if require_write():
            return redirect(url_for("team.retrait"))
        try:
            user = _get_user_or_fail(request.form.get("user_id"))
            amount = parse_amount(request.form.get("amount", ""))
            T.create_withdrawal(
                operator_label=operator(), campus=campus(), user=user, amount_cents=amount
            )
            flash(
                f"Retrait de {amount / 100:.2f} € pour {user.display_name} enregistré.", "success"
            )
            return redirect(url_for("team.retrait"))
        except (T.OperationError, ValueError) as e:
            flash(getattr(e, "message", "Montant invalide."), "danger")
    return render_template("team/operation.html", op="retrait", read_only=not can_write())


@bp.route("/transfert", methods=["GET", "POST"])
@login_required
def transfert():
    if request.method == "POST":
        if require_write():
            return redirect(url_for("team.transfert"))
        try:
            srcs = [_get_user_or_fail(raw) for raw in request.form.getlist("from_id")]
            dsts = [_get_user_or_fail(raw) for raw in request.form.getlist("to_id")]
            amount = parse_amount(request.form.get("amount", ""))
            T.create_transfer(
                operator_label=operator(),
                campus=campus(),
                from_users=srcs,
                to_users=dsts,
                amount_cents=amount,
            )
            flash(
                f"Transfert de {amount / 100:.2f} € : {len(srcs)} donneur(s) → "
                f"{len(dsts)} receveur(s) effectué.",
                "success",
            )
            return redirect(url_for("team.transfert"))
        except (T.OperationError, ValueError) as e:
            flash(getattr(e, "message", "Transfert invalide."), "danger")
    return render_template("team/operation.html", op="transfert", read_only=not can_write())


def _next_target():
    """Cible `next` d'un formulaire, uniquement si c'est un chemin interne sûr."""
    target = request.form.get("next")
    return target if is_safe_target(target) else url_for("team.payment")


def _get_user_or_fail(raw_id):
    try:
        uid = T.id_or_invalid(raw_id, "Sélectionnez un étudiant dans la liste.")
    except T.OperationError:
        raise T.OperationError("invalid", "Sélectionnez un étudiant dans la liste.") from None
    u = db.session.get(User, uid)
    if u is None:
        raise T.OperationError("invalid", "Étudiant introuvable.")
    return u


@bp.route("/consignes/retour", methods=["POST"])
@login_required
def consigne_return():
    if require_write():
        return redirect(_next_target())
    try:
        user = _get_user_or_fail(request.form.get("user_id"))
        count = int(request.form.get("count", "1"))
        t = T.return_glasses(operator_label=operator(), campus=campus(), user=user, count=count)
        flash(
            f"{t.deposit_glasses} verre(s) rendu(s) : {t.total / 100:.2f} € crédités à {user.display_name}.",
            "success",
        )
    except (T.OperationError, ValueError) as e:
        flash(getattr(e, "message", "Erreur."), "danger")
    return redirect(_next_target())


@bp.route("/historique")
@login_required
def historique():
    ftype = request.args.get("type", "")
    fcampus = request.args.get("campus", "")
    fuser = request.args.get("user", "").strip()
    ffrom = request.args.get("from", "")
    fto = request.args.get("to", "")
    query = T.visible_transactions()
    if ftype == EVENT_SALES_FILTER:
        query = query.filter(
            Transaction.type.in_(("achat", "direct")),
            Transaction.lines.any(TransactionLine.article_type == "evenement"),
        )
    elif ftype:
        query = query.filter(Transaction.type == ftype)
    if fcampus in ("brest", "paris"):
        query = query.filter(Transaction.campus == fcampus)
    if ffrom:
        try:
            d = datetime.strptime(ffrom, "%Y-%m-%d")
            query = query.filter(Transaction.created_at >= d)
        except ValueError:
            pass
    if fto:
        try:
            d = datetime.strptime(fto, "%Y-%m-%d") + timedelta(days=1, microseconds=-1)
            query = query.filter(Transaction.created_at <= d)
        except ValueError:
            pass
    if fuser:
        like = f"%{fuser}%"
        ids = set(
            db.session.scalars(
                select(User.id).where(
                    or_(
                        User.name.ilike(like),
                        User.nickname.ilike(like),
                    )
                )
            ).all()
        )
        if ids:
            query = query.filter(Transaction.contributions.any(Contribution.user_id.in_(ids)))
        else:
            query = query.filter(db.false())
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 50
    total = query.order_by(None).count()
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = query.offset((page - 1) * per_page).limit(per_page).all()
    operators = plain_names({t.operator_label for t in rows}, 255)
    return render_template(
        "team/historique.html",
        rows=rows,
        filters=request.args,
        type_filters=HISTORY_TYPE_FILTERS,
        operators=operators,
        describe=lambda t: T.describe_transaction(t, plain=True),
        page=page,
        pages=pages,
        total=total,
        link_args={k: v for k, v in request.args.items() if k != "page"},
    )


@bp.route("/historique/annuler", methods=["POST"])
@login_required
def annuler():
    """Annulation d'une ou plusieurs transactions (sélection multiple, U8)."""
    raw_ids = request.form.getlist("transaction_ids")
    single = request.form.get("transaction_id", "")
    if not raw_ids and single.isascii() and single.isdigit():
        raw_ids = [single]
    # ASCII et longueur bornée : « ² » passe isdigit() mais int() le refuse, et
    # un entier géant déborderait la colonne PostgreSQL.
    ids = [int(x) for x in raw_ids if x.isascii() and x.isdigit() and len(x) <= 9][:50]
    if not ids:
        flash("Aucune transaction sélectionnée.", "danger")
        return redirect(url_for("team.historique"))
    password = request.form.get("admin_password", "")
    if not AP.verify(
        password, own_campus(), f"annulation de {len(ids)} transaction(s) depuis l'historique"
    ):
        flash("Mot de passe administrateur requis pour annuler une transaction.", "danger")
        return redirect(url_for("team.historique"))
    cancelled, errors = 0, []
    for tid in ids:
        t = db.session.get(Transaction, tid)
        if t is None:
            errors.append(f"#{tid} introuvable")
            continue
        if t.cancelled:
            errors.append(f"#{tid} déjà annulée")
            continue
        if t.campus != own_campus():
            errors.append(f"#{tid} : autre campus")
            continue
        try:
            T.cancel_transaction(t, None, verified=True)
            cancelled += 1
        except T.OperationError as e:
            errors.append(f"#{tid} : {e.message}")
    if cancelled:
        flash(
            f"{cancelled} transaction{'s' if cancelled > 1 else ''} annulée"
            f"{'s' if cancelled > 1 else ''}, soldes mis à jour.",
            "success",
        )
    for message in errors[:5]:
        flash(f"Annulation impossible — {message}.", "danger")
    return redirect(url_for("team.historique"))


def _stats_int(value, default, minimum=None, maximum=None):
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _stats_request():
    """Filtres communs à la page et à l'export CSV des statistiques."""
    session_campus = session.get("campus")
    if session_campus not in CAMPUSSES:
        session_campus = "brest"
    campus = request.args.get("campus", session_campus)
    if campus not in ("brest", "paris", "tous"):
        campus = session_campus

    preset = request.args.get("preset", "")
    if preset not in ST.PERIOD_PRESETS:
        preset = (
            "custom" if (request.args.get("date_from") or request.args.get("date_to")) else "30j"
        )
    period = ST.resolve_period(
        preset,
        request.args.get("date_from", ""),
        request.args.get("date_to", ""),
        campus if campus in CAMPUSSES else "",
    )

    category = request.args.get("category", "").strip()[:50]
    promotion = request.args.get("promotion", "").strip()
    promotion_value = ST.promotion_int(promotion)
    promotion = str(promotion_value) if promotion_value is not None else ""
    team_only = request.args.get("team_only", "")
    if team_only not in STATS_TEAM_FILTERS:
        team_only = ""
    events = request.args.get("events", "")
    if events not in STATS_EVENT_FILTERS:
        events = ""

    filters = {
        "date_from": period["start"].isoformat(),
        "date_to": period["end"].isoformat(),
        "category": category,
        "promotion": promotion,
        "campus": campus if campus in CAMPUSSES else "",
        "team_only": team_only,
        "events": events,
    }
    base_args = {"preset": period["preset"], "campus": campus}
    if period["preset"] == "custom":
        base_args["date_from"] = period["start"].isoformat()
        base_args["date_to"] = period["end"].isoformat()
    for key, value in (
        ("category", category),
        ("promotion", promotion),
        ("team_only", team_only),
        ("events", events),
    ):
        if value:
            base_args[key] = value

    previous_start, previous_end = ST.previous_period(period["start"], period["end"])
    return {
        "session_campus": session_campus,
        "campus": campus,
        "filters": filters,
        "previous_filters": {
            **filters,
            "date_from": previous_start.isoformat(),
            "date_to": previous_end.isoformat(),
        },
        "period": period,
        "base_args": base_args,
        "previous_start": previous_start,
        "previous_end": previous_end,
        "previous_period_label": ST.format_period(previous_start, previous_end),
    }


@bp.route("/statistiques")
@login_required
def statistiques():
    context = _stats_request()
    session_campus = context["session_campus"]
    campus = context["campus"]
    filters = context["filters"]
    period = context["period"]
    base_args = context["base_args"]
    previous_start = context["previous_start"]
    previous_end = context["previous_end"]

    vue = request.args.get("vue", "vue")
    if vue not in STATS_VIEWS:
        vue = "vue"
    page = _stats_int(request.args.get("page"), 1, minimum=1)

    totals = ST.period_totals(filters)
    previous_totals = ST.period_totals(context["previous_filters"])
    average_basket = (
        round(totals["revenue"] / totals["transactions"]) if totals["transactions"] else 0
    )
    previous_average_basket = (
        round(previous_totals["revenue"] / previous_totals["transactions"])
        if previous_totals["transactions"]
        else 0
    )
    kpis = [
        {
            "key": "revenue",
            "label": "Recettes",
            "value": totals["revenue"],
            "format": "eur",
            "hint": "hors consignes",
            "delta": ST.percent_delta(totals["revenue"], previous_totals["revenue"]),
        },
        {
            "key": "qty",
            "label": "Articles vendus",
            "value": totals["qty"],
            "format": "int",
            "hint": "sur la période",
            "delta": ST.percent_delta(totals["qty"], previous_totals["qty"]),
        },
        {
            "key": "basket",
            "label": "Panier moyen",
            "value": average_basket,
            "format": "eur",
            "hint": f"{totals['transactions']} transaction"
            f"{'s' if totals['transactions'] > 1 else ''}",
            "delta": ST.percent_delta(average_basket, previous_average_basket),
        },
        {
            "key": "students",
            "label": "Étudiants",
            "value": totals["students"],
            "format": "int",
            "hint": "acheteurs uniques",
            "delta": ST.percent_delta(totals["students"], previous_totals["students"]),
        },
    ]

    def stats_args(**overrides):
        args = dict(base_args, vue=vue)
        for key, value in overrides.items():
            if value in (None, ""):
                args.pop(key, None)
            else:
                args[key] = value
        return args

    filters_count = sum(
        1
        for value in (
            filters["category"],
            filters["promotion"],
            filters["team_only"],
            filters["events"],
        )
        if value
    )
    reset_url = url_for(
        "team.statistiques",
        **stats_args(
            preset="30j",
            campus=session_campus,
            date_from=None,
            date_to=None,
            category=None,
            promotion=None,
            team_only=None,
            events=None,
        ),
    )
    chips = []
    if filters["category"]:
        chips.append(
            {
                "label": ARTICLE_TYPES.get(filters["category"], filters["category"]),
                "url": url_for("team.statistiques", **stats_args(category=None)),
            }
        )
    if filters["promotion"]:
        chips.append(
            {
                "label": f"Promo {filters['promotion']}",
                "url": url_for("team.statistiques", **stats_args(promotion=None)),
            }
        )
    if filters["team_only"]:
        chips.append(
            {
                "label": STATS_TEAM_FILTERS[filters["team_only"]],
                "url": url_for("team.statistiques", **stats_args(team_only=None)),
            }
        )
    if filters["events"]:
        chips.append(
            {
                "label": STATS_EVENT_FILTERS[filters["events"]],
                "url": url_for("team.statistiques", **stats_args(events=None)),
            }
        )
    if campus == "tous":
        chips.append(
            {
                "label": "Les deux campus",
                "url": url_for("team.statistiques", **stats_args(campus=session_campus)),
            }
        )
    elif campus != session_campus:
        chips.append(
            {
                "label": CAMPUSSES[campus],
                "url": url_for("team.statistiques", **stats_args(campus=session_campus)),
            }
        )

    max_category_revenue = max(
        (data["revenue"] for data in totals["by_category"].values()), default=0
    )
    categories = []
    for key, data in totals["by_category"].items():
        categories.append(
            {
                "key": key,
                "label": ARTICLE_TYPES.get(key, key),
                "qty": data["qty"],
                "revenue": data["revenue"],
                "share": round(data["revenue"] * 100 / totals["revenue"])
                if totals["revenue"]
                else 0,
                "average": round(data["revenue"] / data["qty"]) if data["qty"] else 0,
                "bar": round(data["revenue"] * 100 / max_category_revenue)
                if max_category_revenue
                else 0,
                "url": url_for("team.statistiques", **stats_args(vue="articles", category=key)),
            }
        )

    tab_urls = {key: url_for("team.statistiques", vue=key, **base_args) for key in STATS_VIEWS}

    chart_data = None
    trend = None
    events = []
    comparing = False
    compare_url = url_for("team.statistiques", vue="vue", **base_args)
    compare_note = ""
    if vue == "vue":
        series = ST.daily_series(filters, period["start"], period["end"])
        granularity = ST.granularity_for(period["start"], period["end"])
        bucketed = ST.bucket_series(series, period["start"], period["end"], granularity)
        comparing = request.args.get("comparer") == "1" and granularity == "jour"
        compare_args = dict(base_args, vue="vue")
        if not comparing:
            compare_args["comparer"] = "1"
        compare_url = url_for("team.statistiques", **compare_args)
        previous_series = None
        if comparing:
            previous_series = ST.daily_series(
                context["previous_filters"], previous_start, previous_end
            )
            if not any(previous_series["revenue"]):
                compare_note = "Aucune vente sur la période précédente."
        events = ST.events_in_period(period["start"], period["end"], filters["campus"])
        best_revenue = max(bucketed["revenue"], default=0)
        best_day = None
        if best_revenue:
            best_index = bucketed["revenue"].index(best_revenue)
            best_day = {
                "label": bucketed["best_labels"][best_index],
                "revenue": best_revenue,
            }
        days = (period["end"] - period["start"]).days + 1
        buckets = len(bucketed["labels"]) or 1
        trend = {
            "average": round(totals["revenue"] / buckets),
            "average_unit": {
                "jour": "jour",
                "semaine": "semaine",
                "mois": "mois",
            }[granularity],
            "best": best_day,
            "days": days,
            "granularity": granularity,
            "granularity_label": {
                "jour": "Jour par jour, jours sans vente inclus",
                "semaine": "Semaine par semaine, jours sans vente inclus",
                "mois": "Mois par mois, jours sans vente inclus",
            }[granularity],
            "best_label": {
                "jour": "meilleur jour",
                "semaine": "meilleure semaine",
                "mois": "meilleur mois",
            }[granularity],
        }
        chart_data = {
            "dates": bucketed["dates"],
            "labels": bucketed["labels"],
            "tooltips": bucketed["tooltips"],
            "granularity": granularity,
            "revenue": bucketed["revenue"],
            "qty": bucketed["qty"],
            "transactions": bucketed["transactions"],
            "monthly": series["monthly"],
            "events": []
            if granularity != "jour"
            else [
                {"date": day, "name": event["name"]} for event in events for day in event["dates"]
            ],
            "previous": None
            if previous_series is None
            else {
                "dates": previous_series["dates"],
                "revenue": previous_series["revenue"],
                "qty": previous_series["qty"],
                "transactions": previous_series["transactions"],
            },
        }

    article_search = request.args.get("aq", "").strip()[:100]
    article_sort = request.args.get("tri", "volume")
    if article_sort not in STATS_ARTICLE_SORTS:
        article_sort = "volume"
    article_per_page = _stats_int(request.args.get("aper_page"), 25, 10, 100)
    article_data = None
    article_link_args = dict(
        base_args, vue="articles", tri=article_sort, aper_page=article_per_page
    )
    if article_search:
        article_link_args["aq"] = article_search
    article_sort_urls = {
        key: url_for("team.statistiques", **dict(article_link_args, tri=key))
        for key in STATS_ARTICLE_SORTS
    }
    article_search_clear_url = url_for(
        "team.statistiques",
        **{key: value for key, value in article_link_args.items() if key != "aq"},
    )
    if vue == "articles":
        article_data = ST.articles_stats(
            filters,
            search=article_search,
            sort=article_sort,
            page=page,
            per_page=article_per_page,
        )
        offset = (article_data["page"] - 1) * article_per_page
        for index, article in enumerate(article_data["rows"], start=1):
            article["rank"] = offset + index
            article["type_label"] = ARTICLE_TYPES.get(article["type"], article["type"])
            article["share"] = (
                round(article["revenue"] * 100 / totals["revenue"]) if totals["revenue"] else 0
            )
            article["average"] = round(article["revenue"] / article["qty"]) if article["qty"] else 0
            article["bar"] = (
                round(article["revenue"] * 100 / article_data["max_revenue"])
                if article_data["max_revenue"]
                else 0
            )
        article_data["link_args"] = article_link_args

    student_search = request.args.get("q", "").strip()[:100]
    student_sort = request.args.get("tri_etudiants", "depense")
    if student_sort not in STATS_STUDENT_SORTS:
        student_sort = "depense"
    student_per_page = _stats_int(request.args.get("per_page"), 25, 10, 100)
    students = None
    student_link_args = dict(
        base_args,
        vue="etudiants",
        tri_etudiants=student_sort,
        per_page=student_per_page,
    )
    if student_search:
        student_link_args["q"] = student_search
    student_sort_urls = {
        key: url_for("team.statistiques", **dict(student_link_args, tri_etudiants=key))
        for key in STATS_STUDENT_SORTS
    }
    student_search_clear_url = url_for(
        "team.statistiques",
        **{key: value for key, value in student_link_args.items() if key != "q"},
    )
    if vue == "etudiants":
        students = ST.students_stats(
            filters,
            search=student_search,
            sort=student_sort,
            page=page,
            per_page=student_per_page,
        )
        students["search"] = student_search
        students["per_page"] = student_per_page
        students["link_args"] = student_link_args

    export_args = dict(base_args, vue=vue)
    if vue == "articles":
        if article_search:
            export_args["aq"] = article_search
        export_args["tri"] = article_sort
    elif vue == "etudiants":
        if student_search:
            export_args["q"] = student_search
        export_args["tri_etudiants"] = student_sort
    export_url = url_for("team.statistiques_export", **export_args)
    trend_export_url = url_for("team.statistiques_export", vue="serie", **base_args)

    wider_args = {
        key: value for key, value in base_args.items() if key not in ("date_from", "date_to")
    }
    wider_args["preset"] = "12mois"
    wider_url = url_for("team.statistiques", vue=vue, **wider_args)

    promotions = list(
        db.session.scalars(
            select(User.promotion)
            .where(User.promotion.isnot(None))
            .distinct()
            .order_by(User.promotion.desc())
        ).all()
    )
    if filters["promotion"] and int(filters["promotion"]) not in promotions:
        promotions.insert(0, int(filters["promotion"]))

    return render_template(
        "team/statistiques.html",
        vue=vue,
        views=STATS_VIEWS,
        tab_urls=tab_urls,
        filters=filters,
        base_args=base_args,
        preset=period["preset"],
        periods=ST.PERIOD_PRESETS,
        campus=campus,
        campus_label="Les deux campus" if campus == "tous" else CAMPUSSES[campus],
        period_label=ST.format_period(period["start"], period["end"]),
        period_note=period["note"],
        period_event=period["event"],
        updated_at=datetime.now(PARIS_TZ).strftime("%H:%M"),
        totals=totals,
        previous_totals=previous_totals,
        kpis=kpis,
        categories=categories,
        chips=chips,
        filters_count=filters_count,
        reset_url=reset_url,
        promotions=promotions,
        team_filters=STATS_TEAM_FILTERS,
        event_filters=STATS_EVENT_FILTERS,
        chart_data=chart_data,
        trend=trend,
        events=events,
        comparing=comparing,
        compare_url=compare_url,
        compare_note=compare_note,
        previous_period_label=context["previous_period_label"],
        article_data=article_data,
        article_search=article_search,
        article_sort=article_sort,
        article_sort_urls=article_sort_urls,
        article_link_args=article_link_args,
        article_search_clear_url=article_search_clear_url,
        article_per_page=article_per_page,
        students=students,
        student_search=student_search,
        student_sort=student_sort,
        student_sort_urls=student_sort_urls,
        student_search_clear_url=student_search_clear_url,
        export_url=export_url,
        trend_export_url=trend_export_url,
        wider_url=wider_url,
    )


@bp.route("/statistiques/export.csv")
@login_required
def statistiques_export():
    context = _stats_request()
    filters = context["filters"]
    vue = request.args.get("vue", "vue")
    if vue not in STATS_VIEWS and vue != "serie":
        vue = "vue"

    if vue == "serie":
        period = context["period"]
        series = ST.daily_series(filters, period["start"], period["end"])
        granularity = ST.granularity_for(period["start"], period["end"])
        bucketed = ST.bucket_series(series, period["start"], period["end"], granularity)
        rows = []
        for index, start in enumerate(bucketed["starts"]):
            rows.append(
                [
                    start,
                    bucketed["tooltips"][index],
                    f"{bucketed['revenue'][index] / 100:.2f}",
                    bucketed["qty"][index],
                    bucketed["transactions"][index],
                ]
            )
        return _csv_response(
            f"statistiques_serie_{filters['campus'] or 'tous'}_"
            f"{filters['date_from']}_{filters['date_to']}.csv",
            ["Début", "Période", "Recettes (EUR)", "Articles", "Transactions"],
            rows,
        )

    if vue == "articles":
        article_search = request.args.get("aq", "").strip()[:100]
        article_sort = request.args.get("tri", "volume")
        if article_sort not in STATS_ARTICLE_SORTS:
            article_sort = "volume"
        total_revenue = ST.category_totals(filters)["grand_total"]["revenue"]
        data = ST.articles_stats(filters, search=article_search, sort=article_sort, per_page=0)
        rows = []
        for rank, article in enumerate(data["rows"], start=1):
            share = round(article["revenue"] * 100 / total_revenue) if total_revenue else 0
            average = round(article["revenue"] / article["qty"]) if article["qty"] else 0
            rows.append(
                [
                    rank,
                    article["name"],
                    ARTICLE_TYPES.get(article["type"], article["type"]),
                    article["qty"],
                    f"{article['revenue'] / 100:.2f}",
                    share,
                    f"{average / 100:.2f}",
                ]
            )
        return _csv_response(
            f"statistiques_articles_{filters['campus'] or 'tous'}_"
            f"{filters['date_from']}_{filters['date_to']}.csv",
            [
                "Rang",
                "Article",
                "Catégorie",
                "Volume",
                "Recettes (EUR)",
                "Part des recettes (%)",
                "Prix moyen (EUR)",
            ],
            rows,
        )

    if vue == "etudiants":
        student_search = request.args.get("q", "").strip()[:100]
        student_sort = request.args.get("tri_etudiants", "depense")
        if student_sort not in STATS_STUDENT_SORTS:
            student_sort = "depense"
        data = ST.students_stats(filters, search=student_search, sort=student_sort, per_page=0)
        rows = [
            [
                student["name"],
                student["promotion"] or "",
                "équipe" if student["is_team"] else "étudiant",
                student["nb"],
                f"{student['articles']:.1f}",
                f"{student['spent'] / 100:.2f}",
                f"{student['average'] / 100:.2f}",
            ]
            for student in data["rows"]
        ]
        return _csv_response(
            f"statistiques_etudiants_{filters['campus'] or 'tous'}_"
            f"{filters['date_from']}_{filters['date_to']}.csv",
            [
                "Étudiant",
                "Promotion",
                "Profil",
                "Achats",
                "Articles",
                "Total dépensé (EUR)",
                "Panier moyen (EUR)",
            ],
            rows,
        )

    totals = ST.category_totals(filters)
    grand_total = totals["grand_total"]
    rows = []
    for key, category in totals["by_category"].items():
        share = (
            round(category["revenue"] * 100 / grand_total["revenue"])
            if grand_total["revenue"]
            else 0
        )
        average = round(category["revenue"] / category["qty"]) if category["qty"] else 0
        rows.append(
            [
                ARTICLE_TYPES.get(key, key),
                category["qty"],
                f"{category['revenue'] / 100:.2f}",
                share,
                f"{average / 100:.2f}",
            ]
        )
    rows.append(
        [
            "TOTAL",
            grand_total["qty"],
            f"{grand_total['revenue'] / 100:.2f}",
            100 if grand_total["revenue"] else 0,
            "",
        ]
    )
    return _csv_response(
        f"statistiques_categories_{filters['campus'] or 'tous'}_"
        f"{filters['date_from']}_{filters['date_to']}.csv",
        [
            "Catégorie",
            "Volume",
            "Recettes (EUR)",
            "Part des recettes (%)",
            "Prix moyen (EUR)",
        ],
        rows,
    )


@bp.route("/tresorerie")
@login_required
def tresorerie():
    require_mandat()
    c = request.args.get("campus", session["campus"])
    if c not in ("brest", "paris"):
        c = "brest"
    report = treasury(c)
    return render_template("team/tresorerie.html", report=report, campus=c)


def _csv_response(filename, header, rows):
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(header)
    writer.writerows(rows)
    data = "﻿" + buf.getvalue()
    return Response(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@bp.route("/tresorerie/export/mensuel")
@login_required
def tresorerie_export_mensuel():
    require_mandat()
    c = request.args.get("campus", session["campus"])
    if c not in ("brest", "paris"):
        abort(404)
    try:
        year = int(request.args.get("year", ""))
        month = int(request.args.get("month", ""))
    except ValueError:
        abort(404)
    entry = next((e for e in treasury(c) if e["year"] == year and e["month"] == month), None)
    if entry is None:
        abort(404)
    rows = [[f"Rapport mensuel — {site_label()} — {CAMPUSSES[c]} — {entry['label']}"], []]
    for key, label in PAYMENT_METHODS.items():
        rows.append(["Rechargements", label, f"{entry['reloads_by_method'][key] / 100:.2f}"])
    rows.append(["Rechargements", "Total rechargements", f"{entry['reloads_total'] / 100:.2f}"])
    for key, label in ARTICLE_TYPES.items():
        rows.append(["Ventes par type", label, f"{entry['sales_by_type'][key] / 100:.2f}"])
    rows.append(["Ventes", "Hors évènements/soirées", f"{entry['sales_other'] / 100:.2f}"])
    rows.append(["Ventes", "Évènements/soirées", f"{entry['sales_events'] / 100:.2f}"])
    rows.append(
        ["Ventes", "Total ventes (consommation virtuelle)", f"{entry['sales_total'] / 100:.2f}"]
    )
    return _csv_response(
        f"rapport_mensuel_{c}_{year}-{month:02d}.csv",
        ["Section", "Détail", "Montant (EUR)"],
        rows,
    )


@bp.route("/tresorerie/export/annuel")
@login_required
def tresorerie_export_annuel():
    require_mandat()
    c = request.args.get("campus", session["campus"])
    if c not in ("brest", "paris"):
        abort(404)
    report = treasury(c)
    rows = []
    totals = {k: 0 for k in PAYMENT_METHODS}
    totals_type = {k: 0 for k in ARTICLE_TYPES}
    tot_reload = tot_other = tot_events = tot_sales = 0
    for e in report:
        row = [e["label"]]
        for key in PAYMENT_METHODS:
            v = e["reloads_by_method"][key]
            totals[key] += v
            row.append(f"{v / 100:.2f}")
        row.append(f"{e['reloads_total'] / 100:.2f}")
        for key in ARTICLE_TYPES:
            v = e["sales_by_type"][key]
            totals_type[key] += v
            row.append(f"{v / 100:.2f}")
        tot_reload += e["reloads_total"]
        tot_other += e["sales_other"]
        tot_events += e["sales_events"]
        tot_sales += e["sales_total"]
        row += [
            f"{e['sales_other'] / 100:.2f}",
            f"{e['sales_events'] / 100:.2f}",
            f"{e['sales_total'] / 100:.2f}",
        ]
        rows.append(row)
    rows.append(
        [
            "TOTAL (12 mois)",
            *[f"{totals[k] / 100:.2f}" for k in PAYMENT_METHODS],
            f"{tot_reload / 100:.2f}",
            *[f"{totals_type[k] / 100:.2f}" for k in ARTICLE_TYPES],
            f"{tot_other / 100:.2f}",
            f"{tot_events / 100:.2f}",
            f"{tot_sales / 100:.2f}",
        ]
    )
    return _csv_response(
        f"rapport_annuel_{c}_12mois_glissants.csv",
        [
            "Mois",
            *[PAYMENT_METHODS[k] for k in PAYMENT_METHODS],
            "Rechargements (total)",
            *[ARTICLE_TYPES[k] for k in ARTICLE_TYPES],
            "Ventes hors évènements/soirées",
            "Ventes évènements/soirées",
            "Ventes (total)",
        ],
        rows,
    )


def site_label():
    from app.services.settings import get_setting

    return get_setting("site_name") or "Foy'z & Bar"


@bp.route("/notes")
@login_required
def notes():
    """Le nombre maximal de post-it paramétré limite l'AFFICHAGE : les notes
    plus anciennes sont conservées en base (R16) et consultables via ?all=1."""
    limit_priv = int_setting("max_postits_private")
    limit_pub = int_setting("max_postits_public")
    show_all = request.args.get("all") == "1"
    # Départage par identifiant : deux notes publiées dans la même microseconde
    # doivent garder un ordre stable (la plus récente d'abord).
    private_query = (
        select(Note)
        .where(Note.is_public.is_(False))
        .order_by(Note.created_at.desc(), Note.id.desc())
    )
    public_query = (
        select(Note)
        .where(Note.is_public.is_(True))
        .order_by(Note.created_at.desc(), Note.id.desc())
    )
    private = db.session.scalars(
        private_query if show_all else private_query.limit(limit_priv)
    ).all()
    public = db.session.scalars(public_query if show_all else public_query.limit(limit_pub)).all()
    total_private = db.session.scalar(select(func.count(Note.id)).where(Note.is_public.is_(False)))
    total_public = db.session.scalar(select(func.count(Note.id)).where(Note.is_public.is_(True)))
    return render_template(
        "team/notes.html",
        private=private,
        public=public,
        public_authors=public_note_authors(public),
        show_all=show_all,
        total_private=total_private,
        total_public=total_public,
    )


@bp.route("/notes/action", methods=["POST"])
@login_required
def notes_action():
    action = request.form.get("action", "")
    scope_public = request.form.get("scope") == "public"
    content = clamp_text((request.form.get("content") or "").strip(), 2000)
    note_id = request.form.get("note_id", "")
    note = (
        db.session.get(Note, int(note_id))
        if note_id.isascii() and note_id.isdigit() and len(note_id) <= 9
        else None
    )

    if action == "create":
        if content:
            # Les post-it publics signent avec le nom réel ; les notes privées
            # gardent le surnom d'usage de l'équipe.
            author_name = g.current_user.name if scope_public else g.current_user.display_name
            n = Note(
                content=content,
                is_public=scope_public,
                author_id=g.current_user.id,
                author_name=author_name[:120],
            )
            db.session.add(n)
            db.session.commit()
            flash("Note publiée.", "success")
    elif action == "update" and note and content:
        note.content = content
        note.updated_at = utcnow()
        db.session.commit()
        flash("Note modifiée.", "success")
    elif action == "delete" and note:
        db.session.delete(note)
        db.session.commit()
        flash("Note supprimée.", "success")
    return redirect(url_for("team.notes"))
