from flask import Blueprint, abort, g, jsonify, request, session

from app.extensions import db
from app.models import Event, User
from app.services import transactions as T
from app.services.ratelimit import SlidingWindowLimiter
from app.services.stats import sales_stats
from app.services.treasury import treasury
from app.utils import CAMPUSSES, client_ip

bp = Blueprint("api", __name__)

# Depuis une session passerelle (lien sans compte) : recherche d'au moins
# 3 caractères, informations réduites au strict nécessaire pour encaisser (pas
# d'identifiant de connexion ; le statut équipe reste exposé, il détermine le
# tarif affiché) et limiteurs dédiés (par événement et par IP) contre
# l'aspiration de l'annuaire.
GATEWAY_MIN_QUERY = 3
_gateway_search_limiter = SlidingWindowLimiter(window_seconds=60, max_requests=120)
_gateway_wallet_limiter = SlidingWindowLimiter(window_seconds=60, max_requests=30)
_GATEWAY_HIDDEN_FIELDS = ("username",)


def _gateway_limited(limiter):
    return limiter.limited(f"{g.gateway_event.id}:{client_ip()}")


def _json_payload():
    """Corps JSON de la requête : un objet, sinon un dictionnaire vide."""
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


# Seuls endpoints lisibles depuis une session passerelle (campus de
# l'événement, aucune écriture : l'encaissement passe par la route dédiée
# /passerelle/<token>/encaisser du blueprint gateway).
GATEWAY_ENDPOINTS = {"api.students", "api.wallet"}


def _gateway_event():
    event_id = session.get("gateway_event_id")
    if not event_id:
        return None
    ev = db.session.get(Event, event_id)
    if ev is None or not ev.is_running:
        return None
    return ev


@bp.before_request
def require_session():
    if g.get("current_user"):
        return
    ev = _gateway_event()
    if ev is None:
        abort(401)
    if request.endpoint not in GATEWAY_ENDPOINTS:
        abort(403)
    g.gateway_event = ev


def read_campus():
    """Campus de lecture : imposé par l'événement en session passerelle,
    sinon campus demandé (ou session) pour l'équipe."""
    ev = getattr(g, "gateway_event", None)
    if ev is not None:
        return ev.campus
    return request.args.get("campus") or session.get("campus") or "brest"


def write_campus():
    """Campus d'encaissement : celui de la session, à condition qu'il soit le
    campus d'appartenance du membre (l'autre campus est en lecture seule).
    Le compte admin global encaisse sur les deux campus."""
    campus = session.get("campus") or "brest"
    if g.current_user.is_super_admin:
        return campus
    own = g.current_user.team_campus
    if own in CAMPUSSES and campus != own:
        return None
    return campus


@bp.route("/students")
def students():
    query = request.args.get("q", "")
    if getattr(g, "gateway_event", None) is None:
        return jsonify(T.search_students(query, campus=read_campus()))
    if _gateway_limited(_gateway_search_limiter):
        return jsonify(ok=False, error="Trop de requêtes, patientez un instant."), 429
    if len(query.strip()) < GATEWAY_MIN_QUERY:
        return jsonify([])
    return jsonify(
        [
            {k: v for k, v in student.items() if k not in _GATEWAY_HIDDEN_FIELDS}
            for student in T.search_students(query, campus=read_campus())
        ]
    )


@bp.route("/wallet/<int:user_id>")
def wallet(user_id):
    in_gateway = getattr(g, "gateway_event", None) is not None
    if in_gateway and _gateway_limited(_gateway_wallet_limiter):
        return jsonify(ok=False, error="Trop de requêtes, patientez un instant."), 429
    u = db.session.get(User, user_id)
    if u is None:
        return jsonify(ok=False, error="Étudiant introuvable."), 404
    # lecture seule : on ne crée pas de portefeuille pour l'autre campus
    w = T.wallet_view(u, read_campus())
    data = {
        "id": u.id,
        "name": u.display_name,
        "balance": w.balance if w else 0,
        "glasses": w.glasses_outstanding if w else 0,
        "blacklist": u.blacklist,
        "blacklist_alcohol": u.blacklist_alcohol,
        "is_team": u.is_team,
    }
    return jsonify(data)


@bp.route("/purchase", methods=["POST"])
def purchase():
    payload = _json_payload()
    campus = write_campus()
    if campus is None:
        return jsonify(
            ok=False,
            error="Campus consulté en lecture seule : connectez-vous sur votre campus d'équipe.",
        ), 403
    try:
        t = T.create_purchase(
            operator_label=g.current_user.display_name,
            campus=campus,
            items=payload.get("items", []),
            contributor_ids=payload.get("contributors", []),
            deposit_glasses=payload.get("deposit_glasses", 0),
            direct=bool(payload.get("direct")),
            payment_method=payload.get("payment_method"),
            admin_password=payload.get("admin_password"),
            idempotency_key=payload.get("idempotency_key"),
        )
        return jsonify(ok=True, transaction_id=t.id, total=t.total)
    except T.OperationError as e:
        return jsonify(ok=False, code=e.code, error=e.message, extra=e.extra), 400


@bp.route("/glasses/return", methods=["POST"])
def glasses_return():
    payload = _json_payload()
    try:
        uid = int(payload.get("user_id"))
    except (TypeError, ValueError, OverflowError):
        return jsonify(ok=False, error="Étudiant introuvable."), 404
    u = db.session.get(User, uid)
    if u is None:
        return jsonify(ok=False, error="Étudiant introuvable."), 404
    campus = write_campus()
    if campus is None:
        return jsonify(
            ok=False,
            error="Campus consulté en lecture seule : connectez-vous sur votre campus d'équipe.",
        ), 403
    try:
        t = T.return_glasses(
            operator_label=g.current_user.display_name,
            campus=campus,
            user=u,
            count=payload.get("count", 0),
        )
        return jsonify(ok=True, total=t.total)
    except T.OperationError as e:
        return jsonify(ok=False, error=e.message), 400


@bp.route("/stats")
def stats():
    filters = {
        k: request.args.get(k, "")
        for k in ("date_from", "date_to", "category", "promotion", "campus", "team_only")
    }
    return jsonify(sales_stats(filters))


@bp.route("/treasury")
def treasury_data():
    c = request.args.get("campus", "brest")
    return jsonify(treasury(c))
