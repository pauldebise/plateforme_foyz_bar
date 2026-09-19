from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import or_, select

from app.extensions import db
from app.models import Article, Event
from app.services import transactions as T
from app.services.ratelimit import SlidingWindowLimiter
from app.services.stats import top_article_ids
from app.utils import client_ip

bp = Blueprint("gateway", __name__)

# Le lien passerelle n'est pas authentifié : on borne son usage par jeton et
# par IP (ouverture de la page et surtout encaissements).
_page_limiter = SlidingWindowLimiter(window_seconds=60, max_requests=120)
_pay_limiter = SlidingWindowLimiter(window_seconds=60, max_requests=60)


def _limiter_key(token):
    return f"{token}:{client_ip()}"


def _too_many(limiter, token, action):
    if limiter.limited(_limiter_key(token)):
        current_app.logger.warning(
            "Passerelle %s : seuil de %s atteint depuis %s", token, action, client_ip()
        )
        return True
    return False


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
    if _too_many(_page_limiter, token, "chargements"):
        abort(429)
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
    # événements). Chaque campus n'encaisse que ses propres articles.
    articles = db.session.scalars(
        select(Article)
        .where(
            Article.active.is_(True),
            Article.campus == ev.campus,
            or_(Article.event_id == ev.id, Article.event_id.is_(None)),
        )
        .order_by(Article.event_id.is_(None), Article.name)
    ).all()
    # un article standard sans prix public sur ce campus est exclu
    articles = [a for a in articles if a.event_id == ev.id or a.price_for(ev.campus) > 0]
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
    if _too_many(_pay_limiter, token, "encaissements"):
        return jsonify(ok=False, error="Trop de requêtes, patientez un instant."), 429
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
            idempotency_key=payload.get("idempotency_key"),
        )
        return jsonify(ok=True, transaction_id=t.id, total=t.total)
    except T.OperationError as e:
        return jsonify(ok=False, code=e.code, error=e.message, extra=e.extra), 400


@bp.route("/passerelle/<token>/quitter", methods=["POST"])
def quitter(token):
    # Nouvelle authentification exigée pour revenir à l'interface équipe
    # (cahier des charges) : la session passerelle est vidée.
    _event_from_token(token)
    session.clear()
    flash("Passerelle fermée. Connectez-vous pour revenir à l'interface équipe.", "info")
    return redirect(url_for("auth.login"))
