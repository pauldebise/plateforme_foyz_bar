from flask import Blueprint, render_template
from sqlalchemy import select

from app.extensions import db
from app.models import Article, Event, Note, Tap, UsefulLink, User
from app.services.settings import get_setting, int_setting
from app.utils import ARTICLE_TYPES, CAMPUSSES, utcnow

bp = Blueprint("public", __name__)


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
        select(Note)
        .where(Note.is_public.is_(True))
        .order_by(Note.created_at.desc(), Note.id.desc())
        .limit(limit)
    ).all()
    return render_template(
        "public/home.html",
        events_by_campus=events_by_campus,
        notes=notes,
        homepage_text=get_setting("homepage_text"),
    )


@bp.route("/catalogue")
def catalogue():
    articles = db.session.scalars(
        select(Article)
        .where(Article.active.is_(True), Article.event_id.is_(None))
        .order_by(Article.name)
    ).all()
    taps = {t.number: t for t in db.session.scalars(select(Tap))}
    grouped = {}
    for a in articles:
        grouped.setdefault(a.article_type, []).append((a, _campus_prices(a, taps)))
    ordered = sorted(grouped.items(), key=lambda kv: list(ARTICLE_TYPES).index(kv[0]))
    return render_template("public/catalogue.html", grouped=ordered)


def _campus_prices(article, taps):
    """Prix standard par campus ; None quand l'article n'existe pas sur le campus.

    Un article issu de la migration n'existe que sur son campus d'origine
    (tous ses prix y valent 0) et un article de tireuse ne concerne que le
    campus de la tireuse : on affiche alors un tiret plutôt qu'un prix.
    """
    if article.is_tap:
        tap = taps.get(article.tap_number)
        if tap is None:
            return {c: None for c in CAMPUSSES}
        price = article.price_for(tap.campus)
        return {c: (price if c == tap.campus else None) for c in CAMPUSSES}
    return {
        c: (
            article.price_for(c)
            if article.price_for(c) or article.price_for(c, team=True)
            else None
        )
        for c in CAMPUSSES
    }


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


@bp.route("/trombinoscopes")
def trombinoscopes():
    """Présentation nominative et visuelle des équipes de mandat (cahier des
    charges, interface publique). Seuls les membres marqués visibles par
    l'équipe apparaissent ; l'édition se fait dans le module développement."""
    members = db.session.scalars(
        select(User)
        .where(User.team_status == "mandat", User.trombinoscope_visible.is_(True))
        .order_by(User.name)
    ).unique().all()
    by_campus = {c: [] for c in CAMPUSSES}
    for member in members:
        if member.team_campus in by_campus:
            by_campus[member.team_campus].append(member)
    return render_template("public/trombinoscopes.html", by_campus=by_campus)
