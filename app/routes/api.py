from flask import Blueprint, g, jsonify, request, session, abort

from app.extensions import db
from app.models import Article, User
from app.services import transactions as T
from app.services.stats import sales_stats
from app.services.treasury import treasury
from app.utils import CAMPUSSES

bp = Blueprint("api", __name__)


@bp.before_request
def require_session():
    if not g.get("current_user"):
        abort(401)


def write_campus():
    """Campus d'encaissement : celui de la session, à condition qu'il soit le
    campus d'appartenance du membre (l'autre campus est en lecture seule)."""
    campus = session.get("campus") or "brest"
    own = g.current_user.team_campus
    if own in CAMPUSSES and campus != own:
        return None
    return campus


@bp.route("/students")
def students():
    campus = request.args.get("campus") or session.get("campus") or "brest"
    return jsonify(T.search_students(request.args.get("q", ""), campus=campus))


@bp.route("/wallet/<int:user_id>")
def wallet(user_id):
    u = db.session.get(User, user_id)
    if u is None:
        return jsonify(ok=False, error="Étudiant introuvable."), 404
    # lecture seule : on ne crée pas de portefeuille pour l'autre campus
    campus = request.args.get("campus") or session.get("campus") or "brest"
    w = T.wallet_view(u, campus)
    return jsonify(
        id=u.id,
        name=u.name,
        balance=w.balance if w else 0,
        glasses=w.glasses_outstanding if w else 0,
        blacklist=u.blacklist,
        blacklist_alcohol=u.blacklist_alcohol,
        is_team=u.is_team,
    )


@bp.route("/purchase", methods=["POST"])
def purchase():
    payload = request.get_json(silent=True) or {}
    campus = write_campus()
    if campus is None:
        return jsonify(ok=False, error="Campus consulté en lecture seule : connectez-vous sur votre campus d'équipe."), 403
    try:
        t = T.create_purchase(
            operator_label=g.current_user.name,
            campus=campus,
            items=payload.get("items", []),
            contributor_ids=payload.get("contributors", []),
            deposit_glasses=payload.get("deposit_glasses", 0),
            direct=bool(payload.get("direct")),
            payment_method=payload.get("payment_method"),
            admin_password=payload.get("admin_password"),
        )
        return jsonify(ok=True, transaction_id=t.id, total=t.total)
    except T.OperationError as e:
        return jsonify(ok=False, code=e.code, error=e.message, extra=e.extra), 400


@bp.route("/glasses/return", methods=["POST"])
def glasses_return():
    payload = request.get_json(silent=True) or {}
    u = db.session.get(User, payload.get("user_id"))
    if u is None:
        return jsonify(ok=False, error="Étudiant introuvable."), 404
    campus = write_campus()
    if campus is None:
        return jsonify(ok=False, error="Campus consulté en lecture seule : connectez-vous sur votre campus d'équipe."), 403
    try:
        t = T.return_glasses(
            operator_label=g.current_user.name,
            campus=campus,
            user=u,
            count=payload.get("count", 0),
        )
        return jsonify(ok=True, total=t.total)
    except T.OperationError as e:
        return jsonify(ok=False, error=e.message), 400


@bp.route("/stats")
def stats():
    filters = {k: request.args.get(k, "") for k in ("date_from", "date_to", "category", "promotion", "campus", "team_only")}
    return jsonify(sales_stats(filters))


@bp.route("/treasury")
def treasury_data():
    c = request.args.get("campus", "brest")
    return jsonify(treasury(c))
