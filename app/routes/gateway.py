from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import or_, select

from app.extensions import db
from app.models import Article, Event, Tap
from app.services import transactions as T
from app.services.stats import top_article_ids
from app.utils import utcnow

bp = Blueprint("gateway", __name__)


@bp.before_request
def gateway_context():
    if session.get("gateway_event_id"):
        ev = db.session.get(Event, session["gateway_event_id"])
        if ev:
            g.gateway_event = ev
            g.gateway_token = ev.token


def _event_from_token(token):
    ev = db.session.scalars(select(Event).where(Event.token == token)).first()
    if ev is None or ev.closed:
        abort(404)
    return ev


@bp.route("/passerelle/<token>")
def gateway(token):
    ev = _event_from_token(token)
    if not ev.is_running:
        return render_template("gateway/indisponible.html", ev=ev), 403
    session.clear()
    session["gateway_event_id"] = ev.id
    import time

    session["last_activity"] = time.time()
    session.permanent = True
    # Catalogue de la passerelle : articles de l'événement + catalogue
    # standard du campus de l'événement (cahier des charges, §Gestion des
    # événements), dans les mêmes conditions de disponibilité que l'onglet
    # Paiement de l'équipe.
    campus_tap_numbers = select(Tap.number).where(Tap.campus == ev.campus)
    articles = db.session.scalars(
        select(Article)
        .where(
            Article.active.is_(True),
            or_(Article.event_id == ev.id, Article.event_id.is_(None)),
            or_(Article.is_tap.is_(False), Article.tap_number.in_(campus_tap_numbers)),
        )
        .order_by(Article.event_id.is_(None), Article.name)
    ).all()
    # un article standard absent du campus (aucun prix public) est exclu
    articles = [
        a for a in articles
        if a.event_id == ev.id or a.price_for(ev.campus) > 0
    ]
    data = []
    rank_of = {aid: i for i, aid in enumerate(top_article_ids(ev.campus))}
    for a in articles:
        item = {
            "id": a.id,
            "name": a.name,
            "type": a.article_type,
            "volume": a.volume_cl,
            "alcohol": a.is_alcohol,
            "event": a.event_id == ev.id,
            "std": a.price_for(ev.campus, False),
            "team": a.price_for(ev.campus, True),
        }
        rank = rank_of.get(a.id)
        if rank is not None:
            item["rank"] = rank
        data.append(item)
    return render_template("gateway/paiement.html", ev=ev, catalog=data)


@bp.route("/passerelle/<token>/encaisser", methods=["POST"])
def encaisser(token):
    ev = _event_from_token(token)
    if not ev.is_running:
        return jsonify(ok=False, error="Événement non accessible."), 403
    payload = request.get_json(silent=True) or {}
    try:
        t = T.create_purchase(
            operator_label=f"Passerelle · {ev.name}",
            campus=ev.campus,
            items=payload.get("items", []),
            contributor_ids=payload.get("contributors", []),
            deposit_glasses=payload.get("deposit_glasses", 0),
            direct=bool(payload.get("direct")),
            payment_method=payload.get("payment_method"),
            event_id=ev.id,
            admin_password=payload.get("admin_password"),
        )
        return jsonify(ok=True, transaction_id=t.id, total=t.total)
    except T.OperationError as e:
        return jsonify(ok=False, code=e.code, error=e.message, extra=e.extra), 400


@bp.route("/passerelle/<token>/quitter", methods=["POST"])
def quitter(token):
    _event_from_token(token)
    session.clear()
    flash("Passerelle fermée.", "info")
    return redirect(url_for("public.home"))
