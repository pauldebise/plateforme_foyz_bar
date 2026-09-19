import contextlib
import math
import os
from datetime import datetime, timedelta

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from sqlalchemy import func, or_, select
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import Article, AuditLog, Event, Keg, LoginLog, Tap, UsefulLink, User
from app.services import audit as A
from app.services import catalog as C
from app.services import settings as S
from app.utils import (
    ARTICLE_TYPES,
    CAMPUSSES,
    cents,
    clamp_text,
    euros,
    login_required,
    new_token,
    paris_to_utc,
    safe_color,
    slug_username,
    utcnow,
)

bp = Blueprint("admin", __name__)


@bp.before_request
def require_mandat():
    if not g.get("current_user"):
        return redirect(url_for("auth.login", next=request.path))
    if g.current_user.team_status != "mandat":
        endpoint = request.endpoint or ""
        tireuse_endpoints = ("admin.tireuse", "admin.keg", "admin.tap")
        if g.current_user.team_status == "ancien" and endpoint.startswith(tireuse_endpoints):
            return None
        abort(403)
    return None


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


@bp.route("/campus/<campus>")
@login_required
def changer_campus(campus):
    """Bascule du campus de travail de l'admin global, seul à disposer de
    droits sur les deux campus (les autres membres suivent leur équipe)."""
    u = g.current_user
    if u is not None and u.is_super_admin and campus in CAMPUSSES:
        session["campus"] = campus
    return redirect(url_for("team.payment"))


@bp.route("/comptes")
@login_required
def comptes():
    q = request.args.get("q", "").strip()
    stmt = select(User).order_by(User.name)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(
                User.name.ilike(like),
                User.nickname.ilike(like),
                User.username.ilike(like),
            )
        )
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 50
    total = db.session.scalar(select(func.count()).select_from(stmt.subquery()))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    users = db.session.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).unique().all()
    return render_template(
        "admin/comptes.html",
        users=users,
        q=q,
        page=page,
        pages=pages,
        total=total,
        link_args={k: v for k, v in request.args.items() if k != "page"},
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
        return redirect(url_for("admin.comptes"))
    # Identifiant fourni, sinon déduit du nom ; toujours normalisé (slug).
    username = slug_username(raw_username or name)
    if not username:
        flash("Identifiant invalide (lettres et chiffres uniquement).", "danger")
        return redirect(url_for("admin.comptes"))
    existing = db.session.scalars(select(User).where(User.username == username)).first()
    if existing:
        flash(f"L'identifiant « {username} » est déjà utilisé.", "danger")
        return redirect(url_for("admin.comptes"))
    u = User(
        name=name,
        nickname=nickname,
        username=username,
        promotion=int(promotion) if promotion.isdigit() else None,
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


@bp.route("/comptes/<int:user_id>", methods=["GET", "POST"])
@login_required
def compte(user_id):
    u = db.session.get(User, user_id)
    if u is None:
        abort(404)
    if request.method == "POST":
        before = {
            "name": u.name,
            "nickname": u.nickname,
            "username": u.username,
            "promotion": u.promotion,
            "blacklist": u.blacklist,
            "blacklist_alcohol": u.blacklist_alcohol,
        }
        new_name = clamp_text((request.form.get("name") or "").strip(), 255)
        if new_name and new_name != u.name:
            u.name = new_name
        new_nickname = clamp_text((request.form.get("nickname") or "").strip(), 255) or None
        u.nickname = new_nickname
        new_username = slug_username(request.form.get("username") or "")
        if not new_username:
            flash("Identifiant invalide (lettres et chiffres uniquement).", "danger")
            return redirect(url_for("admin.compte", user_id=u.id))
        if new_username != u.username:
            existing = db.session.scalars(select(User).where(User.username == new_username)).first()
            if existing:
                flash(f"L'identifiant « {new_username} » est déjà utilisé.", "danger")
                return redirect(url_for("admin.compte", user_id=u.id))
            u.username = new_username
        promotion = request.form.get("promotion", "").strip()
        u.promotion = int(promotion) if promotion.isdigit() else None
        u.blacklist = request.form.get("blacklist") == "on"
        new_ba = request.form.get("blacklist_alcohol") == "on"
        if (
            u.blacklist_alcohol
            and not new_ba
            and not S.check_admin_password(request.form.get("admin_password", ""))
        ):
            flash(
                "Le retrait du statut « blacklist alcool » exige le mot de passe administrateur.",
                "danger",
            )
            return redirect(url_for("admin.compte", user_id=u.id))
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
            changes.append(f"blacklist : {'oui' if u.blacklist else 'non'}")
        if before["blacklist_alcohol"] != u.blacklist_alcohol:
            changes.append(f"blacklist alcool : {'oui' if u.blacklist_alcohol else 'non'}")
        A.record(
            "compte.modification",
            target=f"{u.display_name} ({u.username})",
            details="; ".join(changes) or "aucun changement",
        )
        db.session.commit()
        flash("Profil mis à jour.", "success")
        return redirect(url_for("admin.compte", user_id=u.id))
    return render_template("admin/compte.html", u=u, warnings=_compte_warnings(u))


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


@bp.route("/comptes/<int:user_id>/supprimer", methods=["POST"])
@login_required
def compte_supprimer(user_id):
    u = db.session.get(User, user_id)
    if u is None:
        abort(404)
    if not S.check_admin_password(request.form.get("admin_password", "")):
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
    return redirect(url_for("admin.comptes"))


@bp.route("/equipe")
@login_required
def equipe():
    members = (
        db.session.scalars(select(User).where(User.team_status.is_not(None)).order_by(User.name))
        .unique()
        .all()
    )
    return render_template("admin/equipe.html", members=members, own_campus=own_campus())


@bp.route("/equipe/<int:user_id>", methods=["GET", "POST"])
@login_required
def membre(user_id):
    u = db.session.get(User, user_id)
    if u is None:
        abort(404)
    own = own_campus()
    # chaque équipe gère les membres de son campus ; un membre sans campus
    # attribué peut être rattaché par n'importe quelle équipe ; l'admin
    # global couvre les deux campus
    editable = u.team_campus in (None, own) or u.is_super_admin
    if request.method == "POST":
        if not editable:
            abort(403)
        status = request.form.get("team_status", "")
        if status not in ("mandat", "ancien", ""):
            status = ""
        u.team_status = status or None
        # le membre est rattaché au campus de l'équipe qui le gère ; un
        # rattachement existant est conservé (l'admin global gère les deux
        # campus sans déplacer les membres)
        u.team_campus = (u.team_campus or own) if u.team_status else None
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
                    "mot de passe redéfini" if password else "mot de passe inchangé",
                ]
            ),
        )
        db.session.commit()
        flash("Membre mis à jour.", "success")
        return redirect(url_for("admin.membre", user_id=u.id))
    return render_template("admin/membre.html", u=u, editable=editable, own_campus=own)


@bp.route("/articles")
@login_required
def articles():
    campus = view_campus()
    articles = db.session.scalars(
        select(Article).where(Article.campus == campus).order_by(Article.is_tap, Article.name)
    ).all()
    return render_template(
        "admin/articles.html",
        articles=articles,
        campus=campus,
        writable=campus == own_campus(),
    )


@bp.route("/articles/nouveau", methods=["GET", "POST"])
@login_required
def article_nouveau():
    if request.method == "POST":
        try:
            a = _article_from_form(Article(), own_campus())
        except ValueError:
            flash("Prix invalide : saisissez un montant numérique raisonnable.", "danger")
            return redirect(url_for("admin.article_nouveau"))
        db.session.add(a)
        A.record(
            "article.creation",
            target=a.name,
            details=f"Type {a.article_type}, campus {CAMPUSSES[own_campus()]}",
        )
        db.session.commit()
        flash("Article créé.", "success")
        return redirect(url_for("admin.articles"))
    return render_template("admin/article_form.html", a=None, own=own_campus(), writable=True)


@bp.route("/articles/<int:article_id>", methods=["GET", "POST"])
@login_required
def article(article_id):
    a = db.session.get(Article, article_id)
    if a is None:
        abort(404)
    if a.is_tap or a.event_id:
        flash("Les articles tireuse et évènement se gèrent dans leurs onglets dédiés.", "warning")
        return redirect(url_for("admin.articles"))
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
            flash("Prix invalide : saisissez un montant numérique raisonnable.", "danger")
            return redirect(url_for("admin.article", article_id=a.id))
        A.record(
            "article.modification",
            target=a.name,
            details=f"Actif : {'oui' if a.active else 'non'}, campus {CAMPUSSES[own_campus()]}",
        )
        db.session.commit()
        flash("Article mis à jour.", "success")
        return redirect(url_for("admin.articles"))
    return render_template("admin/article_form.html", a=a, own=a.campus, writable=writable)


@bp.route("/articles/<int:article_id>/supprimer", methods=["POST"])
@login_required
def article_supprimer(article_id):
    a = db.session.get(Article, article_id)
    if a and not a.is_tap and not a.event_id:
        if a.campus != own_campus():
            abort(403)
        a.active = False
        A.record("article.desactivation", target=a.name)
        db.session.commit()
        flash("Article désactivé.", "success")
    return redirect(url_for("admin.articles"))


def _article_from_form(a, writable_campus):
    a.name = clamp_text((request.form.get("name") or "Sans nom").strip(), 255)
    a.article_type = request.form.get("article_type", "biere")
    if a.article_type not in ARTICLE_TYPES:
        a.article_type = "biere"
    volume = request.form.get("volume_cl", "").strip()
    a.volume_cl = int(volume) if volume.isdigit() else None
    a.is_alcohol = request.form.get("is_alcohol") == "on"
    a.active = request.form.get("active", "on") == "on"
    a.campus = writable_campus
    a.price_std = cents(request.form.get("price_std", "0"))
    a.price_team = cents(request.form.get("price_team", "0"))
    return a


@bp.route("/tireuses")
@login_required
def tireuses():
    campus = view_campus()
    writable = campus == own_campus()
    kegs = db.session.scalars(select(Keg).order_by(Keg.active.desc(), Keg.name)).unique().all()
    taps = db.session.scalars(select(Tap).where(Tap.campus == campus).order_by(Tap.number)).all()
    all_taps = db.session.scalars(select(Tap)).all()
    free_kegs = [
        k for k in kegs if k.remaining_l > 0.01 and not any(t.keg_id == k.id for t in all_taps)
    ]
    return render_template(
        "admin/tireuses.html",
        kegs=kegs,
        taps=taps,
        free_kegs=free_kegs,
        campus=campus,
        writable=writable,
        own_campus=own_campus(),
    )


@bp.route("/tireuses/kegs/nouveau", methods=["POST"])
@login_required
def keg_nouveau():
    try:
        keg = _keg_from_form(Keg(), own_campus())
    except ValueError:
        flash("Volume ou degré invalide (nombres positifs attendus).", "danger")
        return redirect(url_for("admin.tireuses"))
    keg.remaining_l = keg.volume_l
    db.session.add(keg)
    A.record(
        "fut.creation",
        target=keg.name,
        details=f"{keg.volume_l:g} L, campus {CAMPUSSES[own_campus()]}",
    )
    db.session.commit()
    flash("Fût enregistré.", "success")
    return redirect(url_for("admin.tireuses"))


@bp.route("/tireuses/kegs/<int:keg_id>", methods=["GET", "POST"])
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
            flash("Volume ou degré invalide (nombres positifs attendus).", "danger")
            return redirect(url_for("admin.keg", keg_id=keg.id))
        A.record(
            "fut.modification",
            target=keg.name,
            details=f"{keg.volume_l:g} L, restant {keg.remaining_l:g} L",
        )
        db.session.commit()
        if request.form.get("refresh_articles") == "on":
            C.refresh_tap_articles(keg, campus=own_campus())
        flash("Fût mis à jour.", "success")
        return redirect(url_for("admin.tireuses"))
    return render_template("admin/keg_form.html", keg=keg, own=own_campus())


@bp.route("/tireuses/kegs/<int:keg_id>/supprimer", methods=["POST"])
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


def _keg_from_form(k, writable_campus):
    k.name = clamp_text((request.form.get("name") or "Sans nom").strip(), 255)
    k.alcohol_degree = _positive_float("alcohol_degree", k.alcohol_degree or 0.0, 100.0)
    k.volume_l = _positive_float("volume_l", k.volume_l or 30.0, 10_000.0)
    k.remaining_l = _positive_float(
        "remaining_l",
        k.remaining_l or 0.0,
        k.volume_l,
    )
    k.active = request.form.get("active", "on") == "on"
    for c in CAMPUSSES:
        row = k.price_row(c)
        if c != writable_campus:
            # les prix de l'autre campus sont consultables mais non modifiables
            continue
        row.price_half_std = cents(request.form.get(f"{c}_half_std", "0"))
        row.price_pint_std = cents(request.form.get(f"{c}_pint_std", "0"))
        row.price_pot_std = cents(request.form.get(f"{c}_pot_std", "0"))
        row.price_half_team = cents(request.form.get(f"{c}_half_team", "0"))
        row.price_pint_team = cents(request.form.get(f"{c}_pint_team", "0"))
        row.price_pot_team = cents(request.form.get(f"{c}_pot_team", "0"))
    return k


@bp.route("/tireuses/taps/nouveau", methods=["POST"])
@login_required
def tap_nouveau():
    name = (request.form.get("name") or "").strip()
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


@bp.route("/tireuses/taps/<int:tap_id>/renommer", methods=["POST"])
@login_required
def tap_renommer(tap_id):
    tap = _campus_tap_or_404(tap_id)
    name = (request.form.get("name") or "").strip()
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


@bp.route("/tireuses/taps/<int:tap_id>/assigner", methods=["POST"])
@login_required
def tap_assigner(tap_id):
    tap = _campus_tap_or_404(tap_id)
    keg = db.session.get(Keg, request.form.get("keg_id", ""))
    if tap and keg:
        C.assign_keg(tap, keg)
        A.record(
            "tireuse.affectation", target=tap.display_name, details=f"Fût {keg.name}", commit=True
        )
        flash(f'Fût "{keg.name}" assigné à {tap.display_name} : catalogue mis à jour.', "success")
    return redirect(url_for("admin.tireuses"))


@bp.route("/tireuses/taps/<int:tap_id>/detacher", methods=["POST"])
@login_required
def tap_detacher(tap_id):
    tap = _campus_tap_or_404(tap_id)
    if tap:
        C.detach_keg(tap)
        A.record("tireuse.detachement", target=tap.display_name, commit=True)
        flash(f"{tap.display_name} libérée.", "success")
    return redirect(url_for("admin.tireuses"))


@bp.route("/tireuses/taps/<int:tap_id>/supprimer", methods=["POST"])
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


@bp.route("/evenements/<int:event_id>", methods=["GET", "POST"])
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
            A.record("evenement.modification", target=ev.name)
        elif action == "add_article":
            try:
                a = Article(
                    name=clamp_text((request.form.get("name") or "Article événement").strip(), 255),
                    article_type="evenement",
                    is_alcohol=request.form.get("is_alcohol") == "on",
                    event_id=ev.id,
                    campus=ev.campus,
                    price_std=cents(request.form.get("price_std", "0")),
                    price_team=cents(request.form.get("price_team", "0")),
                    active=True,
                )
            except ValueError:
                flash("Prix invalide : montants numériques raisonnables attendus.", "danger")
                return redirect(url_for("admin.evenement", event_id=ev.id))
            volume = request.form.get("volume_cl", "").strip()
            a.volume_cl = int(volume) if volume.isdigit() else None
            db.session.add(a)
            A.record("evenement.article_ajout", target=a.name, details=f"Événement {ev.name}")
        elif action == "del_article":
            a = db.session.get(Article, request.form.get("article_id", ""))
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


@bp.route("/journaux")
@login_required
def journaux():
    days = S.int_setting("login_logs_retention_days")
    if days > 0:
        db.session.query(LoginLog).filter(
            LoginLog.created_at < utcnow() - timedelta(days=days)
        ).delete()
        db.session.commit()
    ffrom = request.args.get("from", "")
    fto = request.args.get("to", "")
    fuser = request.args.get("user", "").strip()
    query = LoginLog.query
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
    logs = query.order_by(LoginLog.created_at.desc()).limit(500).all()
    return render_template("admin/journaux.html", logs=logs, filters=request.args)


@bp.route("/audit")
@login_required
def audit():
    days = S.int_setting("audit_logs_retention_days")
    if days > 0:
        db.session.query(AuditLog).filter(
            AuditLog.created_at < utcnow() - timedelta(days=days)
        ).delete()
        db.session.commit()
    faction = request.args.get("action", "").strip()
    fuser = request.args.get("user", "").strip()
    ffrom = request.args.get("from", "")
    fto = request.args.get("to", "")
    stmt = select(AuditLog)
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
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    per_page = 50
    total = db.session.scalar(select(func.count()).select_from(stmt.subquery()))
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    logs = db.session.scalars(stmt.offset((page - 1) * per_page).limit(per_page)).all()
    return render_template(
        "admin/audit.html",
        logs=logs,
        actions=A.ACTION_LABELS,
        filters=request.args,
        page=page,
        pages=pages,
        total=total,
        link_args={k: v for k, v in request.args.items() if k != "page"},
    )


@bp.route("/sante")
@login_required
def sante():
    from app.config import INSTANCE_DIR
    from app.models import Transaction
    from app.services import health as H

    checks = H.system_checks()
    schema = H.schema_state()
    disks = []
    for label, path in (
        ("Téléversements", current_app.config["UPLOAD_FOLDER"]),
        ("Dossier instance", INSTANCE_DIR),
        ("Sauvegardes", current_app.config["BACKUP_DIR"]),
    ):
        info = H.disk_info(path)
        if info:
            disks.append({"label": label, "path": str(path), **info})
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
    last_logins = db.session.scalars(
        select(LoginLog).order_by(LoginLog.created_at.desc()).limit(5)
    ).all()
    return render_template(
        "admin/sante.html",
        checks=checks,
        schema=schema,
        backups=H.backups_info(),
        disks=disks,
        activity=activity,
        version=current_app.config.get("APP_VERSION", "?"),
        last_logins=last_logins,
    )


@bp.route("/module-dev", methods=["GET", "POST"])
@login_required
def module_dev():
    own = own_campus()
    if request.method == "POST":
        action = request.form.get("action", "settings")
        if action == "settings":
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
                if key in request.form:
                    value = request.form[key]
                    # les couleurs sont injectées dans une balise <style> :
                    # seule une valeur #rrggbb est acceptée (pas d'injection CSS)
                    if key.startswith("theme_color"):
                        value = safe_color(value, S.DEFAULTS.get(key, "#804db3"))
                    S.set_setting(key, value)
            # thème/logos/règlements par campus : seul le campus de l'équipe
            # connectée est modifiable
            key_own = f"theme_color_{own}"
            if key_own in request.form:
                S.set_setting(key_own, safe_color(request.form[key_own], S.DEFAULTS[key_own]))
            try:
                S.set_setting(
                    "overdraft_limit_cents", cents(request.form.get("overdraft_limit_cents", "0"))
                )
                S.set_setting(
                    "deposit_value_cents", cents(request.form.get("deposit_value_cents", "0"))
                )
            except Exception:
                pass
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
            flash("Paramètres enregistrés.", "success")
        elif action == "reset_theme_colors":
            for key in ("theme_color_public", f"theme_color_{own}"):
                S.set_setting(key, S.DEFAULTS[key])
            A.record("reglages.couleurs", details=f"Campus {CAMPUSSES[own]}")
            flash("Couleurs réinitialisées aux valeurs par défaut.", "success")
        elif action == "password":
            current = request.form.get("current_password", "")
            new = request.form.get("new_password", "")
            from app.services import passwords

            problem = passwords.validate(new, username="admin", name="administrateur")
            if not S.check_admin_password(current):
                flash("Mot de passe administrateur actuel incorrect.", "danger")
            elif problem:
                flash(problem, "danger")
            else:
                S.set_admin_password(new)
                A.record("reglages.mot_de_passe")
                flash("Mot de passe administrateur modifié.", "success")
        elif action == "add_link":
            label = clamp_text((request.form.get("label") or "").strip(), 160)
            url = clamp_text((request.form.get("url") or "").strip(), 500)
            if label and url.startswith("http"):
                try:
                    position = int(request.form.get("position") or 0)
                except (TypeError, ValueError):
                    position = 0
                db.session.add(UsefulLink(label=label, url=url, position=position))
                A.record("lien.ajout", target=label, details=url)
                db.session.commit()
                flash("Lien ajouté.", "success")
        elif action == "del_link":
            try:
                link_id = int(request.form.get("link_id", ""))
            except (TypeError, ValueError):
                link_id = None
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
    return render_template("admin/module_dev.html", values=values, links=links, own_campus=own)
