import csv
import io
from datetime import datetime, timedelta

from flask import Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import or_, select

from app.extensions import db
from app.models import Article, Contribution, Note, Tap, Transaction, User
from app.routes.auth import login  # noqa: F401
from app.services import transactions as T
from app.services.settings import int_setting
from app.services.stats import sales_stats, students_stats, top_article_ids
from app.services.treasury import treasury
from app.utils import ARTICLE_TYPES, CAMPUSSES, PAYMENT_METHODS, login_required, utcnow

bp = Blueprint("team", __name__)


@bp.before_request
def require_team_session():
    if not g.get("current_user"):
        return redirect(url_for("auth.login", next=request.path))
    if not session.get("campus"):
        session.clear()
        return redirect(url_for("auth.login", next=request.path))
    return None


def operator():
    return g.current_user.name


def campus():
    return session["campus"]


@bp.route("/paiement")
@login_required
def payment():
    campus_tap_numbers = select(Tap.number).where(Tap.campus == campus())
    articles = db.session.scalars(
        select(Article)
        .where(
            Article.active.is_(True),
            Article.event_id.is_(None),
            or_(Article.is_tap.is_(False), Article.tap_number.in_(campus_tap_numbers)),
        )
        .order_by(Article.is_tap.desc(), Article.name)
    ).all()
    # un article absent du campus courant (tous ses prix y valent 0) est exclu
    # de l'encaissement ; il reste listé dans l'onglet admin Articles
    articles = [
        a for a in articles
        if a.is_tap or a.price_for(campus()) or a.price_for(campus(), team=True)
    ]
    data = []
    rank_of = {aid: i for i, aid in enumerate(top_article_ids(campus()))}
    for a in articles:
        item = {
            "id": a.id,
            "name": a.name,
            "type": a.article_type,
            "volume": a.volume_cl,
            "alcohol": a.is_alcohol,
            "tap": a.is_tap,
            "tap_number": a.tap_number,
            "std": a.price_for(campus(), False),
            "team": a.price_for(campus(), True),
        }
        rank = rank_of.get(a.id)
        if rank is not None:
            item["rank"] = rank
        data.append(item)
    return render_template("team/payment.html", catalog=data)


@bp.route("/rechargement", methods=["GET", "POST"])
@login_required
def rechargement():
    if request.method == "POST":
        try:
            user = _get_user_or_fail(request.form.get("user_id"))
            amount = int(float(request.form.get("amount", "0").replace(",", ".")) * 100)
            t = T.create_reload(
                operator_label=operator(), campus=campus(), user=user,
                amount_cents=amount, payment_method=request.form.get("method", ""),
            )
            flash(f"Rechargement de {t.total / 100:.2f} € pour {user.name} enregistré.", "success")
            return redirect(url_for("team.rechargement"))
        except (T.OperationError, ValueError) as e:
            flash(getattr(e, "message", "Montant invalide."), "danger")
    return render_template("team/operation.html", op="rechargement")


@bp.route("/retrait", methods=["GET", "POST"])
@login_required
def retrait():
    if request.method == "POST":
        try:
            user = _get_user_or_fail(request.form.get("user_id"))
            amount = int(float(request.form.get("amount", "0").replace(",", ".")) * 100)
            T.create_withdrawal(operator_label=operator(), campus=campus(), user=user, amount_cents=amount)
            flash(f"Retrait de {amount / 100:.2f} € pour {user.name} enregistré.", "success")
            return redirect(url_for("team.retrait"))
        except (T.OperationError, ValueError) as e:
            flash(getattr(e, "message", "Montant invalide."), "danger")
    return render_template("team/operation.html", op="retrait")


@bp.route("/transfert", methods=["GET", "POST"])
@login_required
def transfert():
    if request.method == "POST":
        try:
            src = _get_user_or_fail(request.form.get("from_id"))
            dst = _get_user_or_fail(request.form.get("to_id"))
            amount = int(float(request.form.get("amount", "0").replace(",", ".")) * 100)
            T.create_transfer(operator_label=operator(), campus=campus(), from_user=src, to_user=dst, amount_cents=amount)
            flash(f"Transfert de {amount / 100:.2f} € de {src.name} vers {dst.name} effectué.", "success")
            return redirect(url_for("team.transfert"))
        except (T.OperationError, ValueError) as e:
            flash(getattr(e, "message", "Transfert invalide."), "danger")
    return render_template("team/operation.html", op="transfert")


def _get_user_or_fail(raw_id):
    try:
        uid = int(raw_id)
    except (TypeError, ValueError):
        raise T.OperationError("invalid", "Sélectionnez un étudiant dans la liste.")
    u = db.session.get(User, uid)
    if u is None:
        raise T.OperationError("invalid", "Étudiant introuvable.")
    return u


@bp.route("/consignes/retour", methods=["POST"])
@login_required
def consigne_return():
    try:
        user = _get_user_or_fail(request.form.get("user_id"))
        count = int(request.form.get("count", "1"))
        t = T.return_glasses(operator_label=operator(), campus=campus(), user=user, count=count)
        flash(f"{t.deposit_glasses} verre(s) rendu(s) : {t.total / 100:.2f} € crédités à {user.name}.", "success")
    except (T.OperationError, ValueError) as e:
        flash(getattr(e, "message", "Erreur."), "danger")
    return redirect(request.form.get("next") or url_for("team.payment"))


@bp.route("/historique")
@login_required
def historique():
    ftype = request.args.get("type", "")
    fcampus = request.args.get("campus", "")
    fuser = request.args.get("user", "").strip()
    ffrom = request.args.get("from", "")
    fto = request.args.get("to", "")
    query = T.visible_transactions()
    if ftype:
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
            db.session.scalars(select(User.id).where(User.name.ilike(like))).all()
        )
        if ids:
            query = query.filter(Transaction.contributions.any(Contribution.user_id.in_(ids)))
        else:
            query = query.filter(db.false())
    rows = query.limit(300).all()
    return render_template(
        "team/historique.html",
        rows=rows,
        filters=request.args,
        describe=T.describe_transaction,
    )


@bp.route("/historique/annuler", methods=["POST"])
@login_required
def annuler():
    tid = request.form.get("transaction_id")
    t = db.session.get(Transaction, tid) if tid and tid.isdigit() else None
    if t is None:
        flash("Transaction introuvable.", "danger")
        return redirect(url_for("team.historique"))
    try:
        T.cancel_transaction(t, request.form.get("admin_password", ""))
        flash(f"Transaction #{t.id} annulée, soldes mis à jour.", "success")
    except T.OperationError as e:
        flash(e.message, "danger")
    return redirect(url_for("team.historique"))


@bp.route("/statistiques")
@login_required
def statistiques():
    filters = {
        "date_from": request.args.get("date_from", ""),
        "date_to": request.args.get("date_to", ""),
        "category": request.args.get("category", ""),
        "promotion": request.args.get("promotion", ""),
        "campus": request.args.get("campus", ""),
        "team_only": request.args.get("team_only", ""),
    }
    stats = sales_stats(filters)
    students = students_stats(filters)
    return render_template(
        "team/statistiques.html",
        stats=stats,
        students=students,
        filters=filters,
    )


@bp.route("/tresorerie")
@login_required
def tresorerie():
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
    for key, label in ARTICLE_TYPES.items():
        rows.append(["Ventes", label, f"{entry['sales_by_type'][key] / 100:.2f}"])
    rows.append(["Ventes", "Total consommation virtuelle", f"{entry['sales_total'] / 100:.2f}"])
    rows.append(["Événements", "Recettes", f"{entry['events_total'] / 100:.2f}"])
    rows.append(["Total entrées", "Rechargements + événements", f"{entry['grand_total'] / 100:.2f}"])
    return _csv_response(
        f"rapport_mensuel_{c}_{year}-{month:02d}.csv",
        ["Section", "Détail", "Montant (EUR)"],
        rows,
    )


@bp.route("/tresorerie/export/annuel")
@login_required
def tresorerie_export_annuel():
    c = request.args.get("campus", session["campus"])
    if c not in ("brest", "paris"):
        abort(404)
    report = treasury(c)
    rows = []
    totals = {k: 0 for k in PAYMENT_METHODS}
    totals_type = {k: 0 for k in ARTICLE_TYPES}
    tot_reload = tot_sales = tot_events = tot_entries = 0
    for e in report:
        row = [e["label"]]
        for key in PAYMENT_METHODS:
            v = e["reloads_by_method"][key]
            totals[key] += v
            row.append(f"{v / 100:.2f}")
        for key in ARTICLE_TYPES:
            v = e["sales_by_type"][key]
            totals_type[key] += v
            row.append(f"{v / 100:.2f}")
        tot_reload += e["reloads_total"]
        tot_sales += e["sales_total"]
        tot_events += e["events_total"]
        tot_entries += e["grand_total"]
        row += [f"{e['sales_total'] / 100:.2f}", f"{e['events_total'] / 100:.2f}", f"{e['grand_total'] / 100:.2f}"]
        rows.append(row)
    rows.append([
        "TOTAL (12 mois)",
        *[f"{totals[k] / 100:.2f}" for k in PAYMENT_METHODS],
        *[f"{totals_type[k] / 100:.2f}" for k in ARTICLE_TYPES],
        f"{tot_sales / 100:.2f}", f"{tot_events / 100:.2f}", f"{tot_entries / 100:.2f}",
    ])
    return _csv_response(
        f"rapport_annuel_{c}_12mois_glissants.csv",
        [
            "Mois",
            *[PAYMENT_METHODS[k] for k in PAYMENT_METHODS],
            *[ARTICLE_TYPES[k] for k in ARTICLE_TYPES],
            "Ventes (total)", "Événements", "Total entrées",
        ],
        rows,
    )


def site_label():
    from app.services.settings import get_setting

    return get_setting("site_name") or "Foy'z & Bar"


@bp.route("/notes")
@login_required
def notes():
    limit_priv = int_setting("max_postits_private")
    limit_pub = int_setting("max_postits_public")
    private = db.session.scalars(
        select(Note).where(Note.is_public.is_(False)).order_by(Note.created_at.desc()).limit(limit_priv)
    ).all()
    public = db.session.scalars(
        select(Note).where(Note.is_public.is_(True)).order_by(Note.created_at.desc()).limit(limit_pub)
    ).all()
    return render_template("team/notes.html", private=private, public=public)


@bp.route("/notes/action", methods=["POST"])
@login_required
def notes_action():
    action = request.form.get("action", "")
    scope_public = request.form.get("scope") == "public"
    content = (request.form.get("content") or "").strip()
    note_id = request.form.get("note_id", "")
    note = db.session.get(Note, int(note_id)) if note_id.isdigit() else None

    if action == "create":
        if content:
            n = Note(content=content, is_public=scope_public, author_id=g.current_user.id, author_name=g.current_user.name)
            db.session.add(n)
            db.session.commit()
            _trim_notes(scope_public)
            flash("Note publiée.", "success")
    elif action == "update":
        if note and content:
            note.content = content
            note.updated_at = utcnow()
            db.session.commit()
            flash("Note modifiée.", "success")
    elif action == "delete":
        if note:
            db.session.delete(note)
            db.session.commit()
            flash("Note supprimée.", "success")
    return redirect(url_for("team.notes"))


def _trim_notes(is_public):
    limit = int_setting("max_postits_public" if is_public else "max_postits_private")
    notes = db.session.scalars(
        select(Note).where(Note.is_public.is_(is_public)).order_by(Note.created_at.desc())
    ).all()
    for old in notes[limit:]:
        db.session.delete(old)
    db.session.commit()
