from flask import Blueprint, render_template, request, redirect, session, url_for, g
from sqlalchemy import select

from app.extensions import db
from app.models import Article, Event, Note, UsefulLink, User
from app.services.settings import get_setting, int_setting
from app.utils import ARTICLE_TYPES, CAMPUSSES, utcnow

bp = Blueprint("public", __name__)


@bp.before_request
def pick_campus():
    if "campus" in request.args and request.args["campus"] in CAMPUSSES:
        session["public_campus"] = request.args["campus"]
    session.setdefault("public_campus", "brest")


def current_public_campus():
    return session.get("public_campus", "brest")


@bp.route("/")
def home():
    now_events = db.session.scalars(
        select(Event)
        .where(Event.closed.is_(False), Event.ends_at >= utcnow())
        .order_by(Event.starts_at)
    ).all()
    events_by_campus = {c: [] for c in CAMPUSSES}
    for e in now_events:
        if e.campus in events_by_campus:
            events_by_campus[e.campus].append(e)
    limit = int_setting("max_postits_public")
    notes = db.session.scalars(
        select(Note).where(Note.is_public.is_(True)).order_by(Note.created_at.desc()).limit(limit)
    ).all()
    return render_template(
        "public/home.html",
        events_by_campus=events_by_campus,
        notes=notes,
        homepage_text=get_setting("homepage_text"),
    )


@bp.route("/catalogue")
def catalogue():
    campus = current_public_campus()
    articles = db.session.scalars(
        select(Article)
        .where(Article.active.is_(True), Article.event_id.is_(None))
        .order_by(Article.name)
    ).all()
    grouped = {}
    for a in articles:
        grouped.setdefault(a.article_type, []).append(a)
    ordered = sorted(grouped.items(), key=lambda kv: list(ARTICLE_TYPES).index(kv[0]))
    return render_template("public/catalogue.html", grouped=ordered, campus=campus)


@bp.route("/reglement")
def reglement():
    return render_template(
        "public/reglement.html",
        pdfs={
            "brest": get_setting("regulation_pdf_brest"),
            "paris": get_setting("regulation_pdf_paris"),
        },
    )


@bp.route("/liens")
def liens():
    links = db.session.scalars(select(UsefulLink).order_by(UsefulLink.position, UsefulLink.id)).all()
    return render_template("public/liens.html", links=links)


@bp.route("/trombinoscope")
def trombinoscope():
    members = db.session.scalars(
        select(User)
        .where(User.team_status == "mandat")
        .order_by(User.team_campus, User.last_name, User.first_name)
    ).all()
    by_campus = {c: [] for c in CAMPUSSES}
    for m in members:
        by_campus.setdefault(m.team_campus or "brest", []).append(m)
    return render_template("public/trombinoscope.html", by_campus=by_campus)
