import contextlib
import csv
import io
import math
import os
from datetime import datetime, timedelta
from functools import wraps

from flask import (
    Blueprint,
    Response,
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
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import aliased
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import (
    ApiToken,
    Article,
    AuditLog,
    Event,
    Keg,
    KegPrice,
    LoginLog,
    Tap,
    Transaction,
    TransactionLine,
    UsefulLink,
    User,
    Wallet,
)
from app.services import adminpw as AP
from app.services import api_tokens as AT
from app.services import audit as A
from app.services import catalog as C
from app.services import christmas
from app.services import settings as S
from app.services.event_recap import build_recap, export_rows as event_recap_export_rows
from app.services.names import plain_details, plain_names
from app.services.stats import TOP_ARTICLES_DAYS, top_article_sales
from app.utils import (
    ARTICLE_TYPES,
    CAMPUSSES,
    cents,
    clamp_text,
    euros,
    login_required,
    new_token,
    paris_to_utc,
    parse_amount,
    safe_color,
    slug_username,
    to_paris,
    utcnow,
)

bp = Blueprint("admin", __name__)

# Pages ouvertes à tous les membres de l'équipe (mandats et anciens) : les
# tireuses, les comptes, les articles et les événements, onglets sortis de la
# section administrateur. Le reste de /admin/* demeure réservé aux mandats.
MEMBER_ENDPOINTS = (
    "admin.tireuse",
    "admin.keg",
    "admin.tap",
    "admin.compte",
    "admin.article",
    "admin.evenement",
)


@bp.before_request
def require_mandat():
    if not g.get("current_user"):
        return redirect(url_for("auth.login", next=request.path))
    if g.current_user.team_status != "mandat" and not g.current_user.is_super_admin:
        endpoint = request.endpoint or ""
        if g.current_user.team_status == "ancien" and endpoint.startswith(MEMBER_ENDPOINTS):
            return None
        abort(403)
    return None


def super_admin_required(view):
    """Réservé au compte admin global : les mandats n'ont pas accès aux jetons."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.get("current_user") or not g.current_user.is_super_admin:
            abort(403)
        return view(*args, **kwargs)

    return wrapped


def save_upload(file_storage, allowed=None):
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(file_storage.filename)
    if allowed and not filename.lower().endswith(tuple(allowed)):
        flash("Type de fichier non autorisé.", "danger")
        return None
    name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{new_token()[:8]}_{filename}"
    file_storage.save(os.path.join(current_app.config["UPLOAD_FOLDER"], name))
    return name


def session_campus():
    campus = session.get("campus")
    return campus if campus in CAMPUSSES else "brest"


def own_campus():
    """Campus d'appartenance du membre : seul campus où il peut écrire,
    même s'il est connecté sur l'autre campus (lecture seule). Le compte
    admin global fait exception : il écrit sur le campus qu'il consulte."""
    u = g.current_user
    if u is not None and u.is_super_admin:
        return view_campus()
    if u is not None and u.team_campus in CAMPUSSES:
        return u.team_campus
    return session_campus()


def view_campus():
    """Campus affiché sur les pages de gestion : le campus de la connexion
    sauf consultation explicite de l'autre campus via ?campus=… — la page
    est en lecture seule dès qu'il diffère du campus d'appartenance. Pour
    l'admin global, consulter un campus en fait le campus de travail."""
    campus = request.args.get("campus")
    if campus in CAMPUSSES:
        u = g.current_user
        if u is not None and u.is_super_admin:
            session["campus"] = campus
        return campus
    return session_campus()


# Mémoire de la dernière liste consultée (filtres, tri, page) : après une
# modification ou une suppression, on revient sur la même vue au lieu de
# repartir d'une liste vierge. Liste blanche, valeurs courtes : jamais d'URL
# libre (pas de redirection ouverte) et un cookie de session qui reste petit.
LIST_KEYS = {
    "comptes": ("q", "promo", "equipe", "restriction", "tri", "page", "campus"),
    "articles": ("q", "type", "volume", "prix_min", "prix_max", "tri", "campus"),
}


def _remember_list(name):
    session[f"liste_{name}"] = {
        key: value[:100] for key in LIST_KEYS[name] if (value := request.args.get(key, "").strip())
    }


@bp.app_template_global("list_url")
def list_url(name):
    """URL de la liste `name` avec les filtres de la dernière consultation."""
    saved = session.get(f"liste_{name}")
    args = {}
    if isinstance(saved, dict):
        args = {
            key: value
            for key, value in saved.items()
            if key in LIST_KEYS[name] and isinstance(value, str)
        }
    return url_for(f"admin.{name}", **args)


def _render_list_page(template, rows_template, footer_template, **context):
    """Page complète, ou fragment JSON pour le filtrage instantané.

    Les listes Comptes et Articles se mettent à jour à chaque frappe sans
    recharger la page : le script `admin_filter.js` demande alors uniquement
    les lignes et le pied de liste, rendus par les mêmes includes que la page
    complète (aucune duplication du balisage).
    """
    if request.headers.get("X-Foyz-Partiel") == "1":
        return jsonify(
            rows_html=render_template(rows_template, **context),
            footer_html=render_template(footer_template, **context),
        )
    return render_template(template, **context)


@bp.route("/campus/<campus>")
@login_required
def changer_campus(campus):
    """Bascule du campus de travail de l'admin global, seul à disposer de
    droits sur les deux campus (les autres membres suivent leur équipe)."""
    u = g.current_user
    if u is not None and u.is_super_admin and campus in CAMPUSSES:
        session["campus"] = campus
    return redirect(url_for("team.payment"))


COMPTES_EQUIPE = {
    "": "Tous",
    "membre": "Équipe (mandat ou ancien)",
    "mandat": "Mandat en cours",
    "ancien": "Anciens",
    "aucun": "Non membres",
}
COMPTES_RESTRICTION = {
    "": "Toutes",
    "oui": "Au moins une restriction",
    "blacklist": "Blacklist",
    "alcool": "Blacklist alcool",
    "aucune": "Aucune restriction",
}
COMPTES_TRI = {
    "nom": "Nom (A → Z)",
    "brest_asc": "Solde Brest croissant",
    "brest_desc": "Solde Brest décroissant",
    "paris_asc": "Solde Paris croissant",
    "paris_desc": "Solde Paris décroissant",
}


@bp.route("/comptes")
@login_required
def comptes():
    _remember_list("comptes")
    q = request.args.get("q", "").strip()
    equipe = request.args.get("equipe", "")
    restriction = request.args.get("restriction", "")
    tri = request.args.get("tri", "nom")
    promo = request.args.get("promo", "").strip()
    if equipe not in COMPTES_EQUIPE:
        equipe = ""
    if restriction not in COMPTES_RESTRICTION:
        restriction = ""
    if tri not in COMPTES_TRI:
        tri = "nom"
    if promo != "aucune":
        promo = str(_optional_int(promo, maximum=3000) or "")

    stmt = select(User)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                User.name.ilike(like),
                User.nickname.ilike(like),
                User.username.ilike(like),
            )
        )
    if promo == "aucune":
        stmt = stmt.where(User.promotion.is_(None))
    elif promo:
        stmt = stmt.where(User.promotion == int(promo))
    if equipe == "membre":
        stmt = stmt.where(User.team_status.in_(("mandat", "ancien")))
    elif equipe in ("mandat", "ancien"):
        stmt = stmt.where(User.team_status == equipe)
    elif equipe == "aucun":
        stmt = stmt.where(
            or_(User.team_status.is_(None), User.team_status.notin_(("mandat", "ancien")))
        )
    if restriction == "oui":
        stmt = stmt.where(or_(User.blacklist.is_(True), User.blacklist_alcohol.is_(True)))
    elif restriction == "blacklist":
        stmt = stmt.where(User.blacklist.is_(True))
    elif restriction == "alcool":
        stmt = stmt.where(User.blacklist_alcohol.is_(True))
    elif restriction == "aucune":
        stmt = stmt.where(User.blacklist.is_(False), User.blacklist_alcohol.is_(False))

    if tri == "nom":
        stmt = stmt.order_by(User.name, User.id)
    else:
        campus, sens = tri.split("_")
        # Jointure externe : un compte sans portefeuille sur ce campus compte pour 0.
        wallet = aliased(Wallet)
        stmt = stmt.outerjoin(wallet, and_(wallet.user_id == User.id, wallet.campus == campus))
        solde = func.coalesce(wallet.balance, 0)
        stmt = stmt.order_by(solde.asc() if sens == "asc" else solde.desc(), User.name, User.id)

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 50
    total = db.session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    users = db.session.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).unique().all()
    promotions = db.session.scalars(
        select(User.promotion)
        .where(User.promotion.is_not(None))
        .distinct()
        .order_by(User.promotion.desc())
    ).all()
    return _render_list_page(
        "admin/comptes.html",
        "admin/_comptes_rows.html",
        "admin/_comptes_footer.html",
        users=users,
        q=q,
        equipe=equipe,
        restriction=restriction,
        tri=tri,
        promo=promo,
        promotions=promotions,
        options_equipe=COMPTES_EQUIPE,
        options_restriction=COMPTES_RESTRICTION,
        options_tri=COMPTES_TRI,
        filtres_actifs=bool(q or equipe or restriction or promo or tri != "nom"),
        page=page,
        pages=pages,
        total=total,
        link_args={k: v for k, v in request.args.items() if k != "page" and v != ""},
    )


@bp.route("/comptes/nouveau", methods=["POST"])
@login_required
def comptes_nouveau():
    name = clamp_text((request.form.get("name") or "").strip(), 255)
    nickname = clamp_text((request.form.get("nickname") or "").strip(), 255) or None
    raw_username = (request.form.get("username") or "").strip()
    promotion = request.form.get("promotion", "").strip()
    if not name:
        flash("Nom obligatoire.", "danger")
        return redirect(list_url("comptes"))
    # Identifiant fourni, sinon déduit du nom ; toujours normalisé (slug).
    username = slug_username(raw_username or name)
    if not username:
        flash("Identifiant invalide (lettres et chiffres uniquement).", "danger")
        return redirect(list_url("comptes"))
    existing = db.session.scalars(select(User).where(User.username == username)).first()
    if existing:
        flash(f"L'identifiant « {username} » est déjà utilisé.", "danger")
        return redirect(list_url("comptes"))
    u = User(
        name=name,
        nickname=nickname,
        username=username,
        promotion=_optional_int(promotion, maximum=3000),
    )
    db.session.add(u)
    db.session.flush()
    for c in CAMPUSSES:
        u.wallet(c)
    A.record(
        "compte.creation",
        target=f"{u.display_name} ({u.username})",
        details="Portefeuilles Brest et Paris initialisés",
    )
    db.session.commit()
    flash(
        f"Compte de {u.display_name} créé (identifiant {u.username}, portefeuilles Brest et Paris initialisés).",
        "success",
    )
    return redirect(url_for("admin.compte", user_id=u.id))


@bp.route("/comptes/<int(max=2147483647):user_id>", methods=["GET", "POST"])
@login_required
def compte(user_id):
    u = db.session.get(User, user_id)
    if u is None:
        abort(404)
    if request.method == "POST":
        # Le compte d'administration est intouchable par l'équipe (même en
        # mandat) : nom, identifiant et mot de passe compris.
        protected = u.is_super_admin
        if protected and not g.current_user.is_super_admin:
            abort(403)
        before = {
            "name": u.name,
            "nickname": u.nickname,
            "username": u.username,
            "promotion": u.promotion,
            "blacklist": u.blacklist,
            "blacklist_alcohol": u.blacklist_alcohol,
            "blacklist_reason": u.blacklist_reason,
            "blacklist_alcohol_reason": u.blacklist_alcohol_reason,
        }
        if protected:
            # Compte figé : nom et identifiant « admin » (l'identifiant porte le
            # statut super-admin), pas de surnom, ni blacklist ni blacklist
            # alcool (ce n'est pas une personne consommatrice).
            u.name = "admin"
            u.username = "admin"
            u.nickname = None
            u.blacklist = False
            u.blacklist_alcohol = False
            u.blacklist_by = None
            u.blacklist_alcohol_by = None
            u.blacklist_reason = None
            u.blacklist_alcohol_reason = None
        else:
            new_name = clamp_text((request.form.get("name") or "").strip(), 255)
            if new_name and new_name != u.name:
                u.name = new_name
            u.nickname = clamp_text((request.form.get("nickname") or "").strip(), 255) or None
            new_username = slug_username(request.form.get("username") or "")
            if not new_username:
                flash("Identifiant invalide (lettres et chiffres uniquement).", "danger")
                return redirect(url_for("admin.compte", user_id=u.id))
            if new_username != u.username:
                existing = db.session.scalars(
                    select(User).where(User.username == new_username)
                ).first()
                if existing:
                    flash(f"L'identifiant « {new_username} » est déjà utilisé.", "danger")
                    return redirect(url_for("admin.compte", user_id=u.id))
                u.username = new_username
            promotion = request.form.get("promotion", "").strip()
            u.promotion = _optional_int(promotion, maximum=3000)
            new_bl = request.form.get("blacklist") == "on"
            new_ba = request.form.get("blacklist_alcohol") == "on"
            new_bl_reason = (
                clamp_text((request.form.get("blacklist_reason") or "").strip(), 255) or None
            )
            new_ba_reason = (
                clamp_text((request.form.get("blacklist_alcohol_reason") or "").strip(), 255)
                or None
            )
            removals = [
                (label, origin)
                for was, now, origin, label in (
                    (u.blacklist, new_bl, u.blacklist_by, "blacklist"),
                    (u.blacklist_alcohol, new_ba, u.blacklist_alcohol_by, "blacklist alcool"),
                )
                if was and not now
            ]
            # Seule l'équipe qui a posé une blacklist peut la retirer (l'admin
            # global est exempté) : l'origine est vérifiée avant le mot de passe.
            for label, origin in removals:
                if not _can_lift_blacklist(origin):
                    flash(
                        f"{label.capitalize()} posée par l'équipe de "
                        f"{CAMPUSSES[_blacklist_origin(origin)]} : seul un membre de cette "
                        "équipe peut la retirer.",
                        "danger",
                    )
                    return redirect(url_for("admin.compte", user_id=u.id))
            if removals and not AP.verify(
                request.form.get("admin_password", ""),
                own_campus(),
                f"retrait de blacklist pour {u.name}",
            ):
                flash(
                    "Le retrait d'un statut blacklist exige le mot de passe administrateur.",
                    "danger",
                )
                return redirect(url_for("admin.compte", user_id=u.id))
            if new_bl and not u.blacklist:
                u.blacklist_by = own_campus()
            if not new_bl:
                u.blacklist_by = None
            if new_ba and not u.blacklist_alcohol:
                u.blacklist_alcohol_by = own_campus()
            if not new_ba:
                u.blacklist_alcohol_by = None
            # Le motif suit le statut : il est saisi à l'activation, modifiable
            # par l'équipe d'origine tant que la blacklist est active, et effacé
            # au retrait. L'autre équipe peut le consulter sans le modifier.
            if new_bl:
                if not u.blacklist or _can_lift_blacklist(u.blacklist_by):
                    u.blacklist_reason = new_bl_reason
            else:
                u.blacklist_reason = None
            if new_ba:
                if not u.blacklist_alcohol or _can_lift_blacklist(u.blacklist_alcohol_by):
                    u.blacklist_alcohol_reason = new_ba_reason
            else:
                u.blacklist_alcohol_reason = None
            u.blacklist = new_bl
            u.blacklist_alcohol = new_ba
        if u.blacklist:
            flash(
                "Statut blacklist activé : tous les accès de ce compte (dont équipe) sont retirés.",
                "warning",
            )
        changes = []
        if before["name"] != u.name:
            changes.append(f"nom : {u.name}")
        if before["nickname"] != u.nickname:
            changes.append(f"surnom : {u.nickname or 'aucun'}")
        if before["username"] != u.username:
            changes.append(f"identifiant : {before['username']} -> {u.username}")
        if before["promotion"] != u.promotion:
            changes.append(f"promotion : {u.promotion if u.promotion is not None else 'aucune'}")
        if before["blacklist"] != u.blacklist:
            origine = (
                f" (par {CAMPUSSES[_blacklist_origin(u.blacklist_by)]})" if u.blacklist else ""
            )
            changes.append(f"blacklist : {'oui' if u.blacklist else 'non'}{origine}")
        if before["blacklist_alcohol"] != u.blacklist_alcohol:
            origine = (
                f" (par {CAMPUSSES[_blacklist_origin(u.blacklist_alcohol_by)]})"
                if u.blacklist_alcohol
                else ""
            )
            changes.append(f"blacklist alcool : {'oui' if u.blacklist_alcohol else 'non'}{origine}")
        # Le contenu du motif reste sur la fiche du compte : le journal signale
        # seulement qu'il a été renseigné, modifié ou effacé.
        for key, label in (
            ("blacklist_reason", "motif blacklist"),
            ("blacklist_alcohol_reason", "motif blacklist alcool"),
        ):
            before_reason = before[key]
            after_reason = getattr(u, key)
            if before_reason != after_reason:
                if not after_reason:
                    changes.append(f"{label} : effacé")
                elif not before_reason:
                    changes.append(f"{label} : renseigné")
                else:
                    changes.append(f"{label} : modifié")
        A.record(
            "compte.modification",
            target=f"{u.display_name} ({u.username})",
            details="; ".join(changes) or "aucun changement",
        )
        db.session.commit()
        flash("Profil mis à jour.", "success")
        return redirect(url_for("admin.compte", user_id=u.id))
    return render_template(
        "admin/compte.html",
        u=u,
        warnings=_compte_warnings(u),
        blacklists=_blacklists_view(u),
    )


@bp.route("/comptes/<int(max=2147483647):user_id>/arbitrer-solde", methods=["POST"])
@login_required
def compte_arbitrer_solde(user_id):
    """Arbitrage manuel du solde du campus, réservé aux membres de mandat.

    L'opération ne passe pas par la caisse : elle corrige directement le
    portefeuille du campus d'appartenance. Le mot de passe administrateur du
    campus est exigé et l'ajustement est inscrit dans le journal des actions.
    """
    if g.current_user.team_status != "mandat" and not g.current_user.is_super_admin:
        abort(403)
    u = db.session.get(User, user_id)
    if u is None:
        abort(404)
    # Le compte d'administration global n'a pas de solde à arbitrer.
    if u.is_super_admin:
        abort(403)

    direction = request.form.get("direction", "")
    if direction not in ("credit", "debit"):
        flash("Choisissez d'augmenter ou de diminuer le solde.", "danger")
        return redirect(url_for("admin.compte", user_id=u.id))
    try:
        amount = parse_amount(request.form.get("amount", ""))
    except ValueError:
        flash("Montant invalide (de 0,01 € à 5 000,00 €).", "danger")
        return redirect(url_for("admin.compte", user_id=u.id))

    campus = own_campus()
    if not AP.verify(
        request.form.get("admin_password", ""),
        campus,
        f"arbitrage du solde {CAMPUSSES[campus]} de {u.name}",
    ):
        flash(
            "L'arbitrage du solde exige le mot de passe administrateur du campus.",
            "danger",
        )
        return redirect(url_for("admin.compte", user_id=u.id))

    delta = amount if direction == "credit" else -amount
    wallet = u.wallet(campus)
    db.session.flush()
    # Verrou de ligne : deux arbitrages simultanés ne doivent pas s'écraser.
    wallet = db.session.scalars(
        select(Wallet).where(Wallet.id == wallet.id).with_for_update()
    ).one()
    before = wallet.balance
    wallet.balance = Wallet.balance + delta
    db.session.flush()
    after = wallet.balance
    movement = f"{'+' if delta > 0 else '-'}{euros(abs(delta))}"
    A.record(
        "compte.arbitrage_solde",
        target=f"{u.display_name} ({u.username})",
        details=f"{CAMPUSSES[campus]} : {movement} (solde {euros(before)} -> {euros(after)})",
        campus=campus,
    )
    db.session.commit()
    flash(
        f"Solde {CAMPUSSES[campus]} de {u.display_name} arbitré : {movement} "
        f"(nouveau solde {euros(after)}).",
        "success",
    )
    return redirect(url_for("admin.compte", user_id=u.id))


def _compte_warnings(u):
    warnings = []
    if u.team_status:
        warnings.append("ce compte possède un accès équipe")
    soldes = [f"{CAMPUSSES[w.campus]} : {euros(w.balance)}" for w in u.wallets if w.balance != 0]
    if soldes:
        warnings.append("solde non nul (" + ", ".join(soldes) + ")")
    verres = [CAMPUSSES[w.campus] for w in u.wallets if w.glasses_outstanding]
    if verres:
        warnings.append("verres consignés non rendus (" + ", ".join(verres) + ")")
    return warnings


def _blacklist_origin(campus_key):
    """Équipe d'origine d'une blacklist : les statuts sans origine enregistrée
    (imports legacy, données antérieures au suivi) sont rattachés à Brest."""
    return campus_key or "brest"


def _can_lift_blacklist(campus_key):
    """Retrait d'une blacklist : réservé à l'équipe qui l'a posée, l'admin
    global (au-dessus des équipes) étant exempté."""
    if g.current_user is not None and g.current_user.is_super_admin:
        return True
    return _blacklist_origin(campus_key) == own_campus()


def _blacklists_view(u):
    """Origine et droit de retrait des blacklists pour la fiche compte."""
    return {
        flag: {
            "origin": _blacklist_origin(getattr(u, origin_col)),
            "can_lift": _can_lift_blacklist(getattr(u, origin_col)),
        }
        for flag, origin_col in (
            ("blacklist", "blacklist_by"),
            ("blacklist_alcohol", "blacklist_alcohol_by"),
        )
    }


def _deletion_campus_ok(u):
    """Une équipe ne supprime pas un compte rattaché à l'autre campus.

    Rattaché signifie : membre d'équipe de l'autre campus, ou portefeuille
    (solde ou verres consignés) encore engagé sur l'autre campus. Un compte
    sans aucun engagement peut être supprimé par l'une ou l'autre équipe.
    L'admin global, qui couvre les deux campus, n'est pas concerné.
    """
    if g.current_user is not None and g.current_user.is_super_admin:
        return True
    own = own_campus()
    if u.team_campus and u.team_campus != own:
        return False
    return not any(w.campus != own and (w.balance != 0 or w.glasses_outstanding) for w in u.wallets)


@bp.route("/comptes/<int(max=2147483647):user_id>/supprimer", methods=["POST"])
@login_required
def compte_supprimer(user_id):
    u = db.session.get(User, user_id)
    if u is None:
        abort(404)
    # Le compte d'administration ne peut jamais être supprimé.
    if u.is_super_admin:
        abort(403)
    if not AP.verify(
        request.form.get("admin_password", ""),
        own_campus(),
        f"suppression du compte {u.name}",
    ):
        flash("La suppression d'un compte exige le mot de passe administrateur.", "danger")
        return redirect(url_for("admin.compte", user_id=u.id))
    if not _deletion_campus_ok(u):
        flash(
            "Suppression refusée : ce compte est rattaché à l'autre campus "
            "(équipe, solde ou verres consignés). Demandez à l'équipe concernée.",
            "danger",
        )
        return redirect(url_for("admin.compte", user_id=u.id))
    warnings = _compte_warnings(u)
    if warnings:
        flash("Attention : " + ", ".join(warnings) + ".", "warning")
    name = u.display_name
    A.record(
        "compte.suppression",
        target=name,
        details="Portefeuilles effacés, historique comptable conservé",
    )
    db.session.delete(u)
    db.session.commit()
    flash(
        f"Compte de {name} supprimé : portefeuilles effacés, historique comptable "
        "conservé (les libellés d'opérations passées peuvent encore porter le nom).",
        "success",
    )
    return redirect(list_url("comptes"))


@bp.route("/equipe")
@login_required
def equipe():
    q = request.args.get("q", "").strip()
    stmt = select(User).where(User.team_status.is_not(None))
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                User.name.ilike(like),
                User.nickname.ilike(like),
                User.username.ilike(like),
            )
        )
    members = db.session.scalars(stmt.order_by(User.name)).unique().all()
    return _render_list_page(
        "admin/equipe.html",
        "admin/_equipe_rows.html",
        "admin/_equipe_footer.html",
        members=members,
        own_campus=own_campus(),
        q=q,
        filtres_actifs=bool(q),
    )


@bp.route("/equipe/<int(max=2147483647):user_id>", methods=["GET", "POST"])
@login_required
def membre(user_id):
    u = db.session.get(User, user_id)
    if u is None:
        abort(404)
    own = own_campus()
    admin = g.current_user is not None and g.current_user.is_super_admin
    # chaque équipe gère les membres de son campus ; un membre sans campus
    # attribué peut être rattaché par n'importe quelle équipe. Le compte
    # d'administration, hors équipe, n'est jamais modifiable ici (ni statut,
    # ni campus, ni mot de passe), même par un mandat. Seul l'admin global
    # peut réaffecter un membre à l'autre campus.
    editable = not u.is_super_admin and (admin or u.team_campus in (None, own))
    if request.method == "POST":
        if not editable:
            abort(403)
        status = request.form.get("team_status", "")
        if status not in ("mandat", "ancien", ""):
            status = ""
        u.team_status = status or None
        # le membre est rattaché au campus de l'équipe qui le gère ; un
        # rattachement existant est conservé. L'admin global peut en plus
        # réaffecter le membre à l'autre campus (formulaire avec `campus`).
        campus = request.form.get("campus")
        if admin and campus in CAMPUSSES:
            u.team_campus = campus
        if u.team_status:
            u.team_campus = u.team_campus or own
        else:
            u.team_campus = None
        password = request.form.get("password") or ""
        if password:
            from werkzeug.security import generate_password_hash

            from app.services import passwords

            problem = passwords.validate(
                password,
                username=u.username or "",
                name=u.name or "",
                min_length=passwords.TEAM_MIN_LENGTH,
                require_diversity=False,
            )
            if problem:
                flash(problem, "danger")
                return redirect(url_for("admin.membre", user_id=u.id))
            u.password_hash = generate_password_hash(password)
        A.record(
            "equipe.modification",
            target=f"{u.display_name} ({u.username})",
            details="; ".join(
                [
                    f"statut équipe : {u.team_status or 'aucun'}",
                    (
                        f"campus {CAMPUSSES.get(u.team_campus, '—')}"
                        if u.team_status
                        else "aucun campus"
                    ),
                    "mot de passe redéfini" if password else "mot de passe inchangé",
                ]
            ),
        )
        db.session.commit()
        flash("Membre mis à jour.", "success")
        return redirect(url_for("admin.membre", user_id=u.id))
    return render_template("admin/membre.html", u=u, editable=editable, own_campus=own, admin=admin)


ARTICLES_TRI = {
    "nom": "Nom (A → Z)",
    "prix_asc": "Prix standard croissant",
    "prix_desc": "Prix standard décroissant",
    "volume_asc": "Volume croissant",
    "volume_desc": "Volume décroissant",
    "tendance_desc": "Tendances : les plus vendus d'abord",
    "tendance_asc": "Tendances : les moins vendus d'abord",
}


def _filter_cents(value):
    """Prix saisi dans un filtre : vide, invalide ou négatif -> None (ignoré)."""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        amount = cents(raw)
    except ValueError:
        return None
    return amount if amount >= 0 else None


@bp.route("/articles")
@login_required
def articles():
    _remember_list("articles")
    campus = view_campus()
    q = request.args.get("q", "").strip()
    article_type = request.args.get("type", "")
    volume = request.args.get("volume", "").strip()
    prix_min_raw = request.args.get("prix_min", "").strip()
    prix_max_raw = request.args.get("prix_max", "").strip()
    tri = request.args.get("tri", "nom")
    if article_type not in ARTICLE_TYPES:
        article_type = ""
    if tri not in ARTICLES_TRI:
        tri = "nom"
    if volume != "aucun":
        volume = str(_optional_int(volume) or "")
    prix_min = _filter_cents(prix_min_raw)
    prix_max = _filter_cents(prix_max_raw)

    # Seuls les articles du catalogue permanent sont gérés ici : les articles
    # de tireuse et d'événement ont leurs propres onglets et ne doivent pas
    # encombrer la liste.
    stmt = select(Article).where(
        Article.campus == campus,
        Article.is_tap.is_(False),
        Article.event_id.is_(None),
    )
    if q:
        stmt = stmt.where(Article.name.ilike(f"%{q}%"))
    if article_type:
        stmt = stmt.where(Article.article_type == article_type)
    if volume == "aucun":
        stmt = stmt.where(or_(Article.volume_cl.is_(None), Article.volume_cl == 0))
    elif volume:
        stmt = stmt.where(Article.volume_cl == int(volume))
    if prix_min is not None:
        stmt = stmt.where(Article.price_std >= prix_min)
    if prix_max is not None:
        stmt = stmt.where(Article.price_std <= prix_max)

    if tri == "nom":
        stmt = stmt.order_by(Article.name, Article.id)
    elif tri.startswith("prix"):
        price = Article.price_std.asc() if tri == "prix_asc" else Article.price_std.desc()
        stmt = stmt.order_by(price, Article.name, Article.id)
    elif tri.startswith("volume"):
        # Articles sans volume toujours en dernier, quel que soit le sens.
        no_volume = func.coalesce(Article.volume_cl, 0) == 0
        vol = Article.volume_cl.asc() if tri == "volume_asc" else Article.volume_cl.desc()
        stmt = stmt.order_by(no_volume, vol, Article.name, Article.id)
    else:
        stmt = stmt.order_by(Article.name, Article.id)
    items = db.session.scalars(stmt).all()

    # Tendances : même classement mis en cache que le catalogue de la caisse.
    ranking = top_article_sales(campus)
    sales = dict(ranking)
    if tri.startswith("tendance"):
        rank_of = {aid: i for i, (aid, _qty) in enumerate(ranking)}
        sold = sorted((a for a in items if a.id in rank_of), key=lambda a: rank_of[a.id])
        unsold = [a for a in items if a.id not in rank_of]
        items = sold + unsold if tri == "tendance_desc" else unsold + sold[::-1]

    volumes = db.session.scalars(
        select(Article.volume_cl)
        .where(
            Article.campus == campus,
            Article.is_tap.is_(False),
            Article.event_id.is_(None),
            Article.volume_cl.is_not(None),
            Article.volume_cl > 0,
        )
        .distinct()
        .order_by(Article.volume_cl)
    ).all()
    return _render_list_page(
        "admin/articles.html",
        "admin/_articles_rows.html",
        "admin/_articles_footer.html",
        articles=items,
        campus=campus,
        writable=campus == own_campus(),
        q=q,
        article_type=article_type,
        volume=volume,
        volumes=volumes,
        prix_min=prix_min_raw if prix_min is not None else "",
        prix_max=prix_max_raw if prix_max is not None else "",
        tri=tri,
        options_tri=ARTICLES_TRI,
        sales=sales,
        sales_days=TOP_ARTICLES_DAYS,
        filtres_actifs=bool(
            q
            or article_type
            or volume
            or prix_min is not None
            or prix_max is not None
            or tri != "nom"
        ),
    )


@bp.route("/articles/nouveau", methods=["GET", "POST"])
@login_required
def article_nouveau():
    if request.method == "POST":
        try:
            a = _article_from_form(Article(), own_campus())
        except ValueError:
            flash("Valeurs invalides : vérifiez le prix et le degré d'alcool.", "danger")
            return redirect(url_for("admin.article_nouveau"))
        db.session.add(a)
        A.record(
            "article.creation",
            target=a.name,
            details=f"Type {a.article_type}, campus {CAMPUSSES[own_campus()]}",
        )
        db.session.commit()
        flash("Article créé.", "success")
        return redirect(list_url("articles"))
    return render_template("admin/article_form.html", a=None, own=own_campus(), writable=True)


@bp.route("/articles/<int(max=2147483647):article_id>", methods=["GET", "POST"])
@login_required
def article(article_id):
    a = db.session.get(Article, article_id)
    if a is None:
        abort(404)
    if a.is_tap or a.event_id:
        flash("Les articles tireuse et évènement se gèrent dans leurs onglets dédiés.", "warning")
        return redirect(list_url("articles"))
    # chaque campus gère ses propres articles ; l'autre campus est consultable
    # en lecture seule (l'admin global écrit sur le campus consulté)
    writable = a.campus == own_campus()
    if request.method == "POST":
        if not writable:
            abort(403)
        try:
            _article_from_form(a, own_campus())
        except ValueError:
            db.session.rollback()
            flash("Valeurs invalides : vérifiez le prix et le degré d'alcool.", "danger")
            return redirect(url_for("admin.article", article_id=a.id))
        A.record(
            "article.modification",
            target=a.name,
            details=(
                f"Privé : {'oui' if a.is_private else 'non'}, campus {CAMPUSSES[own_campus()]}"
            ),
        )
        db.session.commit()
        flash("Article mis à jour.", "success")
        return redirect(list_url("articles"))
    last_sale = db.session.scalar(
        select(func.max(Transaction.created_at))
        .join(TransactionLine, TransactionLine.transaction_id == Transaction.id)
        .where(
            TransactionLine.article_id == a.id,
            Transaction.type.in_(("achat", "direct")),
            Transaction.cancelled.is_(False),
        )
    )
    return render_template(
        "admin/article_form.html", a=a, own=a.campus, writable=writable, last_sale=last_sale
    )


@bp.route("/articles/<int(max=2147483647):article_id>/supprimer", methods=["POST"])
@login_required
def article_supprimer(article_id):
    a = db.session.get(Article, article_id)
    if a and not a.is_tap and not a.event_id:
        if a.campus != own_campus():
            abort(403)
        name = a.name
        # Suppression réelle : l'historique des ventes reste lisible car chaque
        # ligne de transaction conserve le nom, le type et le prix de l'article
        # (la clé étrangère passe à NULL via ON DELETE SET NULL).
        db.session.delete(a)
        A.record("article.suppression", target=name)
        db.session.commit()
        flash("Article supprimé. L'historique des ventes est conservé.", "success")
    return redirect(list_url("articles"))


def _optional_int(value, maximum=1_000_000):
    """Paramètre facultatif (volume) : vide, non entier ou hors bornes -> None.
    Ne lève jamais : une valeur géante ferait déborder la colonne entière."""
    raw = (value or "").strip()
    # ASCII seulement : « ² » ou « ٣ » passent isdigit() mais int() les refuse
    # (ou les lit autrement) ; hors bornes -> ignoré.
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 9:
        return None
    number = int(raw)
    return number if number <= maximum else None


def _optional_cents(value):
    """Prix facultatif : vide -> 0 ; une saisie non vide doit rester valide."""
    raw = (value or "").strip()
    return cents(raw) if raw else 0


def _required_cents(field):
    """Prix obligatoire d'un fût : vide refusé, 0 € accepté.

    Le prix nul doit être une saisie explicite de l'opérateur, jamais la
    conséquence d'un champ laissé vide.
    """
    raw = (request.form.get(field) or "").strip()
    if not raw:
        raise ValueError(field)
    amount = cents(raw)
    if amount < 0:
        raise ValueError(field)
    return amount


def _optional_degree(field):
    """Degré d'alcool facultatif : vide = inconnu, sinon 0 < degré <= 100."""
    raw = (request.form.get(field) or "").strip().replace(",", ".")
    if not raw:
        return None
    number = float(raw)  # ValueError -> remontée à l'appelant
    if not math.isfinite(number) or number <= 0 or number > 100:
        raise ValueError(field)
    return number


def _article_from_form(a, writable_campus):
    a.name = clamp_text((request.form.get("name") or "Sans nom").strip(), 255)
    a.article_type = request.form.get("article_type", "biere")
    if a.article_type not in ARTICLE_TYPES:
        a.article_type = "biere"
    a.volume_cl = _optional_int(request.form.get("volume_cl"))
    a.is_alcohol = request.form.get("is_alcohol") == "on"
    a.is_private = request.form.get("is_private") == "on"
    a.alcohol_degree = _optional_degree("alcohol_degree") if a.is_alcohol else None
    a.campus = writable_campus
    a.price_std = _required_cents("price_std")
    a.price_team = _required_cents("price_team")
    return a


@bp.route("/tireuses")
@login_required
def tireuses():
    campus = view_campus()
    writable = campus == own_campus()
    kegs = db.session.scalars(select(Keg).order_by(Keg.name)).unique().all()
    taps = db.session.scalars(select(Tap).where(Tap.campus == campus).order_by(Tap.number)).all()
    return render_template(
        "admin/tireuses.html",
        kegs=kegs,
        taps=taps,
        campus=campus,
        writable=writable,
        own_campus=own_campus(),
    )


@bp.route("/tireuses/kegs/nouveau", methods=["POST"])
@login_required
def keg_nouveau():
    try:
        keg = _keg_from_form(Keg(), own_campus(), all_campuses=True)
    except ValueError:
        db.session.rollback()
        flash(
            "Valeurs invalides : vérifiez le volume, le degré et les prix "
            "(0 € accepté, champ vide refusé).",
            "danger",
        )
        return redirect(url_for("admin.tireuses"))
    db.session.add(keg)
    A.record(
        "fut.creation",
        target=keg.name,
        details=f"{keg.volume_l:g} L, campus {CAMPUSSES[own_campus()]}",
    )
    db.session.commit()
    flash("Fût enregistré.", "success")
    return redirect(url_for("admin.tireuses"))


@bp.route("/tireuses/kegs/<int(max=2147483647):keg_id>", methods=["GET", "POST"])
@login_required
def keg(keg_id):
    keg = db.session.get(Keg, keg_id)
    if keg is None:
        abort(404)
    if request.method == "POST":
        try:
            _keg_from_form(keg, own_campus())
        except ValueError:
            db.session.rollback()
            flash(
                "Valeurs invalides : vérifiez le volume, le degré et les prix "
                "(0 € accepté, champ vide refusé).",
                "danger",
            )
            return redirect(url_for("admin.keg", keg_id=keg.id))
        A.record(
            "fut.modification",
            target=keg.name,
            details=f"{keg.volume_l:g} L",
        )
        db.session.commit()
        # Les articles de tireuse sont toujours régénérés : ils portent le nom
        # et les prix du fût, et une case à cocher oubliée laisserait la caisse
        # sur d'anciens prix.
        C.refresh_tap_articles(keg, campus=own_campus())
        flash("Fût mis à jour.", "success")
        return redirect(url_for("admin.tireuses"))
    return render_template("admin/keg_form.html", keg=keg, own=own_campus())


@bp.route("/tireuses/kegs/<int(max=2147483647):keg_id>/supprimer", methods=["POST"])
@login_required
def keg_supprimer(keg_id):
    keg = db.session.get(Keg, keg_id)
    if keg:
        mounted_elsewhere = [
            t
            for t in db.session.scalars(select(Tap).where(Tap.keg_id == keg.id))
            if t.campus != own_campus()
        ]
        if mounted_elsewhere:
            labels = ", ".join(
                f"{t.display_name} ({CAMPUSSES[t.campus]})" for t in mounted_elsewhere
            )
            flash(
                f"Suppression impossible : ce fût est monté sur {labels}. Demandez à l'équipe concernée de le détacher.",
                "danger",
            )
            return redirect(url_for("admin.tireuses"))
        for tap in list(db.session.scalars(select(Tap).where(Tap.keg_id == keg.id))):
            C.detach_keg(tap)
        A.record("fut.suppression", target=keg.name)
        db.session.delete(keg)
        db.session.commit()
        flash("Fût supprimé.", "success")
    return redirect(url_for("admin.tireuses"))


def _positive_float(field, default, maximum):
    raw = (request.form.get(field) or "").strip().replace(",", ".")
    if not raw:
        return default
    number = float(raw)  # ValueError -> remontée à l'appelant
    if not math.isfinite(number) or number < 0 or number > maximum:
        raise ValueError(field)
    return number


def _keg_from_form(k, writable_campus, *, all_campuses=False):
    """Applique le formulaire fût.

    À la création (`all_campuses=True`), les prix des deux campus sont exigés :
    un prix nul doit être saisi explicitement. À la modification, seul le
    campus de l'opérateur est modifiable, l'autre reste en lecture seule.
    """
    k.name = clamp_text((request.form.get("name") or "Sans nom").strip(), 255)
    k.alcohol_degree = _positive_float("alcohol_degree", k.alcohol_degree or 0.0, 100.0)
    k.volume_l = _positive_float("volume_l", k.volume_l or 30.0, 10_000.0)
    for c in CAMPUSSES:
        row = k.price_row(c)
        if c != writable_campus and not all_campuses:
            # les prix de l'autre campus sont consultables mais non modifiables
            continue
        # À la création, les deux campus sont saisis explicitement : aucun prix
        # ne reste à 0 par simple oubli.
        row.price_half_std = _required_cents(f"{c}_half_std")
        row.price_pint_std = _required_cents(f"{c}_pint_std")
        row.price_pot_std = _required_cents(f"{c}_pot_std")
        row.price_half_team = _required_cents(f"{c}_half_team")
        row.price_pint_team = _required_cents(f"{c}_pint_team")
        row.price_pot_team = _required_cents(f"{c}_pot_team")
    return k


@bp.route("/tireuses/taps/nouveau", methods=["POST"])
@login_required
def tap_nouveau():
    name = clamp_text((request.form.get("name") or "").strip(), 255)
    campus = own_campus()
    if not name:
        flash("Le nom de la tireuse est obligatoire.", "danger")
        return redirect(url_for("admin.tireuses"))
    if db.session.scalars(
        select(Tap).where(func.lower(Tap.name) == name.lower(), Tap.campus == campus)
    ).first():
        flash("Une tireuse porte déjà ce nom sur ce campus.", "danger")
        return redirect(url_for("admin.tireuses"))
    max_number = db.session.scalar(select(func.max(Tap.number))) or 0
    db.session.add(Tap(number=max_number + 1, name=name[:255], campus=campus))
    A.record("tireuse.creation", target=name, details=f"Campus {CAMPUSSES[campus]}")
    db.session.commit()
    flash(f'"{name}" ajoutée.', "success")
    return redirect(url_for("admin.tireuses"))


def _campus_tap_or_404(tap_id):
    tap = db.session.get(Tap, tap_id)
    if tap is None or tap.campus != own_campus():
        return None
    return tap


@bp.route("/tireuses/taps/<int(max=2147483647):tap_id>/renommer", methods=["POST"])
@login_required
def tap_renommer(tap_id):
    tap = _campus_tap_or_404(tap_id)
    name = clamp_text((request.form.get("name") or "").strip(), 255)
    if tap is None:
        abort(404)
    if not name:
        flash("Le nom de la tireuse est obligatoire.", "danger")
        return redirect(url_for("admin.tireuses"))
    duplicate = db.session.scalars(
        select(Tap).where(
            func.lower(Tap.name) == name.lower(), Tap.id != tap.id, Tap.campus == tap.campus
        )
    ).first()
    if duplicate:
        flash("Une autre tireuse porte déjà ce nom sur ce campus.", "danger")
        return redirect(url_for("admin.tireuses"))
    old_name = tap.name
    tap.name = name[:255]
    A.record("tireuse.renommage", target=name, details=f"Ancien nom : {old_name}")
    db.session.commit()
    if tap.keg_id:
        C.assign_keg(tap, db.session.get(Keg, tap.keg_id))
    flash(f'Tireuse renommée : "{name}".', "success")
    return redirect(url_for("admin.tireuses"))


@bp.route("/tireuses/taps/<int(max=2147483647):tap_id>/assigner", methods=["POST"])
@login_required
def tap_assigner(tap_id):
    tap = _campus_tap_or_404(tap_id)
    keg_id = _optional_int(request.form.get("keg_id"), maximum=999_999_999)
    keg = db.session.get(Keg, keg_id) if keg_id is not None else None
    if tap and keg:
        C.assign_keg(tap, keg)
        A.record(
            "tireuse.affectation", target=tap.display_name, details=f"Fût {keg.name}", commit=True
        )
        flash(f'Fût "{keg.name}" assigné à {tap.display_name} : catalogue mis à jour.', "success")
    return redirect(url_for("admin.tireuses"))


@bp.route("/tireuses/taps/<int(max=2147483647):tap_id>/detacher", methods=["POST"])
@login_required
def tap_detacher(tap_id):
    tap = _campus_tap_or_404(tap_id)
    if tap:
        C.detach_keg(tap)
        A.record("tireuse.detachement", target=tap.display_name, commit=True)
        flash(f"{tap.display_name} libérée.", "success")
    return redirect(url_for("admin.tireuses"))


@bp.route("/tireuses/taps/<int(max=2147483647):tap_id>/supprimer", methods=["POST"])
@login_required
def tap_supprimer(tap_id):
    tap = _campus_tap_or_404(tap_id)
    if tap is None:
        abort(404)
    name = tap.display_name
    C.delete_tap(tap)
    A.record(
        "tireuse.suppression",
        target=name,
        details="Articles pression désactivés",
        commit=True,
    )
    flash(f'Tireuse "{name}" supprimée : ses articles pression ont été désactivés.', "success")
    return redirect(url_for("admin.tireuses"))


@bp.route("/evenements")
@login_required
def evenements():
    events = db.session.scalars(select(Event).order_by(Event.starts_at.desc())).all()
    counts = {
        ev.id: len(db.session.scalars(select(Article).where(Article.event_id == ev.id)).all())
        for ev in events
    }
    return render_template(
        "admin/evenements.html", events=events, article_counts=counts, own_campus=own_campus()
    )


@bp.route("/evenements/nouveau", methods=["POST"])
@login_required
def evenement_nouveau():
    name = (request.form.get("name") or "").strip()
    campus = own_campus()
    try:
        starts = paris_to_utc(
            datetime.strptime(request.form.get("starts_at", ""), "%Y-%m-%dT%H:%M")
        )
        ends = paris_to_utc(datetime.strptime(request.form.get("ends_at", ""), "%Y-%m-%dT%H:%M"))
    except ValueError:
        flash("Horaires invalides.", "danger")
        return redirect(url_for("admin.evenements"))
    if not name or ends <= starts:
        flash("Nom et horaires cohérents requis.", "danger")
        return redirect(url_for("admin.evenements"))
    poster = save_upload(request.files.get("poster"), allowed=(".jpg", ".jpeg", ".png", ".webp"))
    ev = Event(
        name=clamp_text(name, 160),
        campus=campus,
        starts_at=starts,
        ends_at=ends,
        token=new_token(),
        poster=poster,
    )
    db.session.add(ev)
    A.record(
        "evenement.creation",
        target=ev.name,
        details=f"Campus {CAMPUSSES[campus]}, du {ev.starts_at:%d/%m/%Y %H:%M} au {ev.ends_at:%d/%m/%Y %H:%M}",
    )
    db.session.commit()
    flash("Événement créé.", "success")
    return redirect(url_for("admin.evenement", event_id=ev.id))


@bp.route("/evenements/<int(max=2147483647):event_id>", methods=["GET", "POST"])
@login_required
def evenement(event_id):
    ev = db.session.get(Event, event_id)
    if ev is None:
        abort(404)
    writable = ev.campus == own_campus() or g.current_user.is_super_admin
    if request.method == "POST":
        if not writable:
            abort(403)
        action = request.form.get("action", "edit")
        if action == "edit":
            ev.name = clamp_text((request.form.get("name") or ev.name).strip(), 160)
            try:
                ev.starts_at = paris_to_utc(
                    datetime.strptime(request.form.get("starts_at", ""), "%Y-%m-%dT%H:%M")
                )
                ev.ends_at = paris_to_utc(
                    datetime.strptime(request.form.get("ends_at", ""), "%Y-%m-%dT%H:%M")
                )
            except ValueError:
                flash("Horaires invalides.", "danger")
                return redirect(url_for("admin.evenement", event_id=ev.id))
            poster = save_upload(
                request.files.get("poster"), allowed=(".jpg", ".jpeg", ".png", ".webp")
            )
            if poster:
                ev.poster = poster
            ev.allow_standard_articles = request.form.get("allow_standard_articles") == "on"
            A.record("evenement.modification", target=ev.name)
        elif action == "add_article":
            try:
                a = Article(
                    name=clamp_text((request.form.get("name") or "Article événement").strip(), 255),
                    article_type="evenement",
                    is_alcohol=request.form.get("is_alcohol") == "on",
                    event_id=ev.id,
                    campus=ev.campus,
                    price_std=_optional_cents(request.form.get("price_std", "0")),
                    price_team=_optional_cents(request.form.get("price_team", "0")),
                    active=True,
                )
            except ValueError:
                flash("Prix invalide : montants numériques raisonnables attendus.", "danger")
                return redirect(url_for("admin.evenement", event_id=ev.id))
            a.volume_cl = _optional_int(request.form.get("volume_cl"))
            db.session.add(a)
            A.record("evenement.article_ajout", target=a.name, details=f"Événement {ev.name}")
        elif action == "del_article":
            article_id = _optional_int(request.form.get("article_id"), maximum=999_999_999)
            a = db.session.get(Article, article_id) if article_id is not None else None
            if a and a.event_id == ev.id:
                A.record(
                    "evenement.article_suppression", target=a.name, details=f"Événement {ev.name}"
                )
                db.session.delete(a)
        elif action == "regenerate_token":
            ev.token = new_token()
            A.record(
                "evenement.jeton",
                target=ev.name,
                details="Les anciens liens de passerelle sont invalidés",
            )
        elif action == "toggle_closed":
            ev.closed = not ev.closed
            A.record(
                "evenement.fermeture",
                target=ev.name,
                details="Événement fermé" if ev.closed else "Événement réouvert",
            )
        db.session.commit()
        flash("Événement mis à jour.", "success")
        return redirect(url_for("admin.evenement", event_id=ev.id))
    temp_articles = db.session.scalars(
        select(Article).where(Article.event_id == ev.id).order_by(Article.name)
    ).all()
    return render_template(
        "admin/evenement.html",
        ev=ev,
        temp_articles=temp_articles,
        writable=writable,
        own_campus=own_campus(),
    )


def _recap_event(event_id):
    """Événement dont le récap est demandé ; None (avec message) s'il n'est
    pas encore terminé ou clôturé."""
    ev = db.session.get(Event, event_id)
    if ev is None:
        abort(404)
    if not ev.is_finished:
        flash(
            "Le récap est disponible une fois l'événement terminé ou clôturé.",
            "warning",
        )
        return None
    return ev


@bp.route("/evenements/<int(max=2147483647):event_id>/recap")
@login_required
def evenement_recap(event_id):
    ev = _recap_event(event_id)
    if ev is None:
        return redirect(url_for("admin.evenement", event_id=event_id))
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    search = (request.args.get("q") or "").strip()[:100]
    return render_template(
        "admin/evenement_recap.html",
        ev=ev,
        recap=build_recap(ev, page=page, search=search),
    )


@bp.route("/evenements/<int(max=2147483647):event_id>/recap.csv")
@login_required
def evenement_recap_export(event_id):
    ev = _recap_event(event_id)
    if ev is None:
        return redirect(url_for("admin.evenement", event_id=event_id))
    header, rows = event_recap_export_rows(ev)
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(header)
    writer.writerows(rows)
    data = "\ufeff" + buffer.getvalue()
    return Response(
        data,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=recap_evenement_{ev.id}.csv"},
    )


def _journal_pagination(stmt, per_page=50):
    """Pagine un `stmt` ordonné ; retourne (lignes, page, pages, total)."""
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    total = db.session.scalar(select(func.count()).select_from(stmt.subquery()))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    rows = db.session.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).all()
    return rows, page, pages, total


def _journal_connexions():
    """Onglet connexions : tentatives de connexion (LoginLog)."""
    ffrom = request.args.get("from", "")
    fto = request.args.get("to", "")
    fuser = request.args.get("user", "").strip()
    query = LoginLog.query
    cutoff = S.display_cutoff("login_logs_retention_days")
    if cutoff is not None:
        query = query.filter(LoginLog.created_at >= cutoff)
    if ffrom:
        with contextlib.suppress(ValueError):
            query = query.filter(LoginLog.created_at >= datetime.strptime(ffrom, "%Y-%m-%d"))
    if fto:
        with contextlib.suppress(ValueError):
            query = query.filter(
                LoginLog.created_at
                <= datetime.strptime(fto, "%Y-%m-%d") + timedelta(days=1, microseconds=-1)
            )
    if fuser:
        query = query.filter(
            LoginLog.name.ilike(f"%{fuser}%")
            | LoginLog.user_id.in_(
                select(User.id).where(
                    or_(
                        User.name.ilike(f"%{fuser}%"),
                        User.nickname.ilike(f"%{fuser}%"),
                        User.username.ilike(f"%{fuser}%"),
                    )
                )
            )
        )
    stmt = query.order_by(LoginLog.created_at.desc(), LoginLog.id.desc())
    rows, page, pages, total = _journal_pagination(stmt)
    return {"logs": rows, "page": page, "pages": pages, "total": total}


def _journal_actions():
    """Onglet actions : actions d'administration sensibles (AuditLog)."""
    faction = request.args.get("action", "").strip()
    fuser = request.args.get("user", "").strip()
    ffrom = request.args.get("from", "")
    fto = request.args.get("to", "")
    stmt = select(AuditLog)
    cutoff = S.display_cutoff("audit_logs_retention_days")
    if cutoff is not None:
        stmt = stmt.where(AuditLog.created_at >= cutoff)
    if faction in A.ACTION_LABELS:
        stmt = stmt.where(AuditLog.action == faction)
    if fuser:
        like = f"%{fuser}%"
        stmt = stmt.where(or_(AuditLog.actor.ilike(like), AuditLog.target.ilike(like)))
    if ffrom:
        with contextlib.suppress(ValueError):
            stmt = stmt.where(AuditLog.created_at >= datetime.strptime(ffrom, "%Y-%m-%d"))
    if fto:
        with contextlib.suppress(ValueError):
            stmt = stmt.where(
                AuditLog.created_at
                <= datetime.strptime(fto, "%Y-%m-%d") + timedelta(days=1, microseconds=-1)
            )
    stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    rows, page, pages, total = _journal_pagination(stmt)
    # Acteurs et cibles des actions sur les comptes sont toujours des libellés
    # de compte : on ne garde que le nom réel, même si le surnom a changé
    # depuis l'écriture de la ligne. Les autres cibles (article, fût, tireuse…)
    # ne sont ramenées à un nom que si le libellé correspond exactement.
    account_targets = {
        row.target for row in rows if row.target and row.action.startswith(("compte.", "equipe."))
    }
    actor_labels = {row.actor for row in rows if row.actor and row.user_id is not None}
    plain = plain_names(
        {row.actor for row in rows if row.actor} | account_targets,
        160,
        user_labels=True,
    )
    # Compte supprimé ou nom réel modifié depuis l'écriture : le préfixe reste
    # le nom réel au moment de l'action, sans le surnom ni l'identifiant.
    for label in actor_labels | account_targets:
        plain.setdefault(label, label.split(" (", 1)[0])
    plain.update(plain_names({row.target for row in rows if row.target} - account_targets, 160))
    return {
        "logs": rows,
        "page": page,
        "pages": pages,
        "total": total,
        "plain": plain,
        "details_plain": {
            row.id: plain_details(row.action, row.details) for row in rows if row.details
        },
    }


@bp.route("/journal")
@login_required
def journal():
    """Journal unifié : onglet connexions (LoginLog) et onglet actions (AuditLog).

    Chaque onglet n'affiche que les N derniers jours (réglage propre à l'onglet) ;
    rien n'est jamais supprimé par la consultation."""
    tab = request.args.get("tab", "connexions")
    if tab not in ("connexions", "actions"):
        tab = "connexions"
    days = S.int_setting(
        "login_logs_retention_days" if tab == "connexions" else "audit_logs_retention_days"
    )
    data = _journal_connexions() if tab == "connexions" else _journal_actions()
    link_args = {**{k: v for k, v in request.args.items() if k != "page"}, "tab": tab}
    return render_template(
        "admin/journal.html",
        tab=tab,
        filters=request.args,
        actions=A.ACTION_LABELS,
        link_args=link_args,
        display_days=days,
        **data,
    )


@bp.route("/journaux")
@login_required
def journaux():
    """Ancienne URL du registre des connexions : redirige vers le journal unifié."""
    return redirect(url_for("admin.journal", **request.args.to_dict()))


@bp.route("/audit")
@login_required
def audit():
    """Ancienne URL du journal d'audit : redirige vers l'onglet actions."""
    args = {k: v for k, v in request.args.items() if k != "tab"}
    return redirect(url_for("admin.journal", tab="actions", **args))


@bp.route("/sante")
@login_required
def sante():
    from app.config import INSTANCE_DIR
    from app.models import Transaction
    from app.services import health as H

    checks = H.system_checks()
    schema = H.schema_state()
    disks = []
    by_device = {}
    for label, path in (
        ("Téléversements", current_app.config["UPLOAD_FOLDER"]),
        ("Dossier instance", INSTANCE_DIR),
        ("Sauvegardes", current_app.config["BACKUP_DIR"]),
    ):
        info = H.disk_info(path)
        if not info:
            continue
        # Plusieurs dossiers sur la même partition : une seule jauge.
        known = by_device.get(info["device"]) if info["device"] is not None else None
        if known:
            known["labels"].append(label)
            continue
        info["labels"] = [label]
        by_device[info["device"]] = info
        disks.append(info)
    activity = {
        "comptes": db.session.scalar(select(func.count()).select_from(User)),
        "membres": db.session.scalar(
            select(func.count()).select_from(User).where(User.team_status.is_not(None))
        ),
        "transactions": db.session.scalar(select(func.count()).select_from(Transaction)),
        "transactions_24h": db.session.scalar(
            select(func.count())
            .select_from(Transaction)
            .where(Transaction.created_at >= utcnow() - timedelta(days=1))
        ),
        "db_size": H.database_size(),
    }
    return render_template(
        "admin/sante.html",
        checks=checks,
        schema=schema,
        backups=H.backups_info(),
        disks=disks,
        activity=activity,
        version=current_app.config.get("APP_VERSION", "?"),
    )


def _api_articles(campus=None):
    stmt = (
        select(Article)
        .where(Article.is_tap.is_(False))
        .order_by(Article.active.desc(), Article.name)
    )
    if campus is not None:
        stmt = stmt.where(Article.campus == campus)
    return db.session.scalars(stmt).all()


def _api_keg_options(campus=None, include_keg_ids=None):
    stmt = select(Tap).order_by(Tap.campus, Tap.number)
    if campus is not None:
        stmt = stmt.where(Tap.campus == campus)
    options = {}
    for tap in db.session.scalars(stmt).all():
        if tap.keg is None:
            continue
        key = (tap.keg_id, tap.campus)
        option = options.get(key)
        if option is None:
            option = {"keg": tap.keg, "campus": tap.campus, "taps": []}
            options[key] = option
        option["taps"].append(tap.number)

    # Fûts non montés : un fût préparé pour la soirée doit pouvoir être
    # sélectionné avant d'être installé sur une tireuse. On l'affilie au campus
    # par ses prix (une ligne KegPrice non nulle), les lignes vides créées pour
    # l'autre campus ne suffisant pas.
    priced = (
        select(KegPrice, Keg)
        .join(Keg, KegPrice.keg_id == Keg.id)
        .where(
            or_(
                KegPrice.price_half_std != 0,
                KegPrice.price_pint_std != 0,
                KegPrice.price_pot_std != 0,
                KegPrice.price_half_team != 0,
                KegPrice.price_pint_team != 0,
                KegPrice.price_pot_team != 0,
            )
        )
    )
    if campus is not None:
        priced = priced.where(KegPrice.campus == campus)
    for price, keg in db.session.execute(priced):
        key = (keg.id, price.campus)
        options.setdefault(key, {"keg": keg, "campus": price.campus, "taps": []})

    # Un fût déjà sélectionné mais détaché de sa tireuse reste visible dans le
    # formulaire, sinon un simple changement de borne le retirerait du jeton.
    if campus is not None:
        for keg_id in include_keg_ids or set():
            key = (keg_id, campus)
            if key in options:
                continue
            keg = db.session.get(Keg, keg_id)
            if keg is not None:
                options[key] = {"keg": keg, "campus": campus, "taps": []}
    return sorted(options.values(), key=lambda item: (item["campus"], item["keg"].name.lower()))


def _api_form_context(campus=None, include_keg_ids=None):
    articles = _api_articles(campus)
    event_articles = [article for article in articles if article.event_id is not None]
    event_ids = {article.event_id for article in event_articles}
    events = (
        {
            event.id: event
            for event in db.session.scalars(select(Event).where(Event.id.in_(event_ids)))
        }
        if event_ids
        else {}
    )
    grouped = {}
    for article in event_articles:
        grouped.setdefault(article.event_id, []).append(article)
    event_groups = [(events.get(event_id), items) for event_id, items in grouped.items()]
    event_groups.sort(
        key=lambda pair: (
            pair[0].starts_at if pair[0] else utcnow(),
            pair[0].name.lower() if pair[0] else "",
        )
    )
    return {
        "standard_articles": [article for article in articles if article.event_id is None],
        "event_groups": event_groups,
        "keg_options": _api_keg_options(campus, include_keg_ids),
    }


def _local_input(value):
    return to_paris(value).strftime("%Y-%m-%dT%H:%M") if value else ""


def _api_form_values(token):
    now = utcnow()
    if token is None:
        return {
            "expires_value": _local_input(now + timedelta(hours=24)),
            "history_from_value": _local_input(now),
            "history_to_value": _local_input(now + timedelta(hours=24)),
        }
    return {
        "expires_value": _local_input(token.expires_at),
        "history_from_value": _local_input(token.history_from),
        "history_to_value": _local_input(token.history_to),
    }


def _api_selection_from_form(campus):
    expires_at = AT.parse_form_datetime(request.form.get("expires_at"), "La date d'expiration")
    history_from = AT.parse_form_datetime(
        request.form.get("history_from"), "La borne de début de l'historique"
    )
    history_to = AT.parse_form_datetime(
        request.form.get("history_to"), "La borne de fin de l'historique"
    )
    articles, kegs = AT.selection_objects(
        campus, request.form.getlist("article_ids"), request.form.getlist("keg_ids")
    )
    return expires_at, history_from, history_to, articles, kegs


@bp.route("/api")
@login_required
@super_admin_required
def api_tokens():
    tokens = db.session.scalars(
        select(ApiToken).order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
    ).all()
    return render_template("admin/api.html", tokens=tokens)


@bp.route("/api/nouveau", methods=["GET", "POST"])
@login_required
@super_admin_required
def api_token_nouveau():
    if request.method == "POST":
        try:
            campus = (request.form.get("campus") or "").strip()
            expires_at, history_from, history_to, articles, kegs = _api_selection_from_form(campus)
            token, raw = AT.create_token(
                label=request.form.get("label", ""),
                campus=campus,
                expires_at=expires_at,
                history_from=history_from,
                history_to=history_to,
                can_read_consumption=request.form.get("can_read_consumption") == "on",
                can_write_prices=request.form.get("can_write_prices") == "on",
                can_write_team_prices=request.form.get("can_write_team_prices") == "on",
                can_read_team_prices=request.form.get("can_read_team_prices") == "on",
                articles=articles,
                kegs=kegs,
                created_by=g.current_user,
            )
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("admin.api_token_nouveau"))
        A.record(
            "api.jeton_creation",
            target=token.label,
            details=(
                f"Campus {CAMPUSSES[token.campus]}, "
                f"{len(token.article_links)} article(s), {len(token.keg_links)} fût(s), "
                f"expire le {to_paris(token.expires_at):%d/%m/%Y %H:%M}"
            ),
        )
        db.session.commit()
        return render_template("admin/api_token_cree.html", token=token, raw_token=raw)

    context = _api_form_context()
    values = _api_form_values(None)
    return render_template(
        "admin/api_form.html",
        token=None,
        campus=own_campus(),
        standard_articles=context["standard_articles"],
        event_groups=context["event_groups"],
        keg_options=context["keg_options"],
        selected_article_ids=set(),
        selected_keg_ids=set(),
        form_action=url_for("admin.api_token_nouveau"),
        title="Nouveau jeton API",
        **values,
    )


@bp.route("/api/<int(max=2147483647):token_id>", methods=["GET", "POST"])
@login_required
@super_admin_required
def api_token(token_id):
    token = db.session.get(ApiToken, token_id)
    if token is None:
        abort(404)
    if request.method == "POST":
        if token.is_revoked:
            flash("Un jeton révoqué ne peut plus être modifié.", "danger")
            return redirect(url_for("admin.api_token", token_id=token.id))
        try:
            expires_at, history_from, history_to, articles, kegs = _api_selection_from_form(
                token.campus
            )
            AT.update_token(
                token,
                label=request.form.get("label", ""),
                expires_at=expires_at,
                history_from=history_from,
                history_to=history_to,
                can_read_consumption=request.form.get("can_read_consumption") == "on",
                can_write_prices=request.form.get("can_write_prices") == "on",
                can_write_team_prices=request.form.get("can_write_team_prices") == "on",
                can_read_team_prices=request.form.get("can_read_team_prices") == "on",
                articles=articles,
                kegs=kegs,
            )
        except ValueError as exc:
            db.session.rollback()
            flash(str(exc), "danger")
            return redirect(url_for("admin.api_token", token_id=token.id))
        A.record(
            "api.jeton_modification",
            target=token.label,
            details=(
                f"{len(token.article_links)} article(s), {len(token.keg_links)} fût(s), "
                f"expire le {to_paris(token.expires_at):%d/%m/%Y %H:%M}"
            ),
        )
        db.session.commit()
        flash(
            "Jeton mis à jour : les changements de sélection et de bornes sont "
            "immédiatement actifs.",
            "success",
        )
        return redirect(url_for("admin.api_tokens"))

    context = _api_form_context(
        token.campus, include_keg_ids={link.keg_id for link in token.keg_links}
    )
    values = _api_form_values(token)
    return render_template(
        "admin/api_form.html",
        token=token,
        campus=token.campus,
        standard_articles=context["standard_articles"],
        event_groups=context["event_groups"],
        keg_options=context["keg_options"],
        selected_article_ids={link.article_id for link in token.article_links},
        selected_keg_ids={link.keg_id for link in token.keg_links},
        form_action=url_for("admin.api_token", token_id=token.id),
        title=f"Modifier le jeton « {token.label} »",
        **values,
    )


@bp.route("/api/<int(max=2147483647):token_id>/revoquer", methods=["POST"])
@login_required
@super_admin_required
def api_token_revoquer(token_id):
    token = db.session.get(ApiToken, token_id)
    if token is None:
        abort(404)
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        A.record("api.jeton_revocation", target=token.label)
        db.session.commit()
        flash("Jeton révoqué : les appels suivants seront refusés.", "success")
    return redirect(url_for("admin.api_tokens"))


SETTING_LABELS = {
    "overdraft_limit_cents": "Découvert autorisé",
    "deposit_value_cents": "Valeur de la consigne",
    "max_history_days": "Durée d'accès aux transactions",
    "login_logs_retention_days": "Affichage du journal de connexion",
    "audit_logs_retention_days": "Affichage du journal d'audit",
    "session_timeout_minutes": "Déconnexion pour inactivité",
    "max_postits_private": "Post-it privés",
    "max_postits_public": "Post-it publics",
}


@bp.route("/module-dev/deco-noel", methods=["POST"])
@login_required
@super_admin_required
def module_dev_deco_noel():
    """Dates du décor de Noël : réglage réservé au compte admin global."""
    start_raw = (request.form.get("christmas_deco_start") or "").strip()
    end_raw = (request.form.get("christmas_deco_end") or "").strip()
    if not start_raw and not end_raw:
        start = end = ""
    else:
        start = christmas.parse_form_date(start_raw)
        end = christmas.parse_form_date(end_raw)
        if start is None or end is None:
            flash(
                "Décor de Noël : saisissez deux dates valides, ou laissez les deux champs vides.",
                "danger",
            )
            return redirect(url_for("admin.module_dev"))
    S.set_setting("christmas_deco_start", start)
    S.set_setting("christmas_deco_end", end)
    A.record("reglages.deco_noel", details=f"du {start or '—'} au {end or '—'}")
    db.session.commit()
    flash(
        "Dates du décor de Noël enregistrées." if start else "Décor de Noël désactivé.",
        "success",
    )
    return redirect(url_for("admin.module_dev"))


@bp.route("/module-dev", methods=["GET", "POST"])
@login_required
def module_dev():
    own = own_campus()
    if request.method == "POST":
        action = request.form.get("action", "settings")
        if action == "settings":
            errors = []
            for key in (
                "overdraft_limit_cents",
                "deposit_value_cents",
                "deposit_enabled",
                "max_history_days",
                "login_logs_retention_days",
                "audit_logs_retention_days",
                "session_timeout_minutes",
                "max_postits_private",
                "max_postits_public",
                "homepage_text",
                "theme_color_public",
                "site_name",
                "link_hosting",
                "link_database",
                "link_repository",
            ):
                if key not in request.form:
                    continue
                value = request.form[key]
                label = SETTING_LABELS.get(key, key)
                # Valeur invalide ou hors bornes : refusée, l'ancienne est conservée.
                if key in S.INT_BOUNDS:
                    low, high = S.INT_BOUNDS[key]
                    try:
                        number = int(value.strip())
                    except ValueError:
                        number = None
                    if number is None or not low <= number <= high:
                        errors.append(f"{label} : entier de {low} à {high} attendu.")
                        continue
                    value = number
                elif key in S.CENTS_BOUNDS:
                    low, high = S.CENTS_BOUNDS[key]
                    try:
                        amount = cents(value)
                    except ValueError:
                        amount = None
                    if amount is None or not low <= amount <= high:
                        errors.append(f"{label} : montant de {euros(low)} à {euros(high)} attendu.")
                        continue
                    value = amount
                # les couleurs sont injectées dans une balise <style> :
                # seule une valeur #rrggbb est acceptée (pas d'injection CSS)
                elif key.startswith("theme_color"):
                    value = safe_color(value, S.DEFAULTS.get(key, "#804db3"))
                S.set_setting(key, value)
            # thème/logos/règlements par campus : seul le campus de l'équipe
            # connectée est modifiable
            key_own = f"theme_color_{own}"
            if key_own in request.form:
                S.set_setting(key_own, safe_color(request.form[key_own], S.DEFAULTS[key_own]))
            pdf_own = save_upload(request.files.get(f"regulation_pdf_{own}"), allowed=(".pdf",))
            # SVG exclu volontairement : un SVG servi sur l'origine peut porter
            # du script (XSS stocké). Formats raster uniquement.
            logo_own = save_upload(
                request.files.get(f"logo_{own}"), allowed=(".jpg", ".jpeg", ".png", ".webp")
            )
            payment_photo_own = save_upload(
                request.files.get(f"payment_photo_{own}"),
                allowed=(".jpg", ".jpeg", ".png", ".webp"),
            )
            if pdf_own:
                S.set_setting(f"regulation_pdf_{own}", pdf_own)
            if logo_own:
                S.set_setting(f"logo_{own}", logo_own)
            if payment_photo_own:
                S.set_setting(f"payment_photo_{own}", payment_photo_own)
            A.record("reglages.modification", details=f"Campus {CAMPUSSES[own]}")
            for error in errors:
                flash(error, "danger")
            flash(
                "Autres paramètres enregistrés." if errors else "Paramètres enregistrés.",
                "warning" if errors else "success",
            )
        elif action == "reset_theme_colors":
            for key in ("theme_color_public", f"theme_color_{own}"):
                S.set_setting(key, S.DEFAULTS[key])
            A.record("reglages.couleurs", details=f"Campus {CAMPUSSES[own]}")
            flash("Couleurs réinitialisées aux valeurs par défaut.", "success")
        elif action == "password":
            current = request.form.get("current_password", "")
            new = request.form.get("new_password", "")
            from app.services import passwords

            problem = passwords.validate(
                new, username="admin", name="administrateur", require_diversity=False
            )
            if not AP.verify(
                current, own, f"changement du mot de passe administrateur ({CAMPUSSES[own]})"
            ):
                flash(
                    f"Mot de passe administrateur actuel incorrect ({CAMPUSSES[own]}).",
                    "danger",
                )
            elif problem:
                flash(problem, "danger")
            else:
                S.set_admin_password(new, own)
                A.record("reglages.mot_de_passe", details=f"campus {CAMPUSSES[own]}")
                flash(f"Mot de passe administrateur modifié ({CAMPUSSES[own]}).", "success")
        elif action == "add_link":
            label = clamp_text((request.form.get("label") or "").strip(), 160)
            url = clamp_text((request.form.get("url") or "").strip(), 500)
            if label and url.startswith("http"):
                try:
                    position = int(request.form.get("position") or 0)
                except (TypeError, ValueError):
                    position = 0
                position = max(-1_000_000, min(1_000_000, position))
                db.session.add(UsefulLink(label=label, url=url, position=position))
                A.record("lien.ajout", target=label, details=url)
                db.session.commit()
                flash("Lien ajouté.", "success")
        elif action == "del_link":
            link_id = _optional_int(request.form.get("link_id"), maximum=999_999_999)
            link = db.session.get(UsefulLink, link_id) if link_id is not None else None
            if link:
                A.record("lien.suppression", target=link.label, details=link.url)
                db.session.delete(link)
                db.session.commit()
                flash("Lien supprimé.", "success")
        db.session.commit()
        return redirect(url_for("admin.module_dev"))
    links = db.session.scalars(select(UsefulLink).order_by(UsefulLink.position)).all()
    values = {k: S.get_setting(k) for k in S.DEFAULTS}
    deco_start, deco_end = christmas.date_input_values(
        values["christmas_deco_start"], values["christmas_deco_end"]
    )
    return render_template(
        "admin/module_dev.html",
        values=values,
        links=links,
        own_campus=own,
        deco_start=deco_start,
        deco_end=deco_end,
    )
