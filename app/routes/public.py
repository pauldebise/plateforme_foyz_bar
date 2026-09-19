from flask import Blueprint, render_template
from sqlalchemy import select

from app.extensions import db
from app.models import Article, Event, Note, Tap, UsefulLink
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
    """Prix public par campus ; None quand l'article n'existe pas sur le campus.

    Chaque article appartient à un unique campus (catalogues Brest et Paris
    distincts) : son prix n'apparaît que dans sa colonne, l'autre voit un
    tiret. Un article de tireuse ne concerne que le campus de sa tireuse.
    """
    if article.is_tap:
        tap = taps.get(article.tap_number)
        if tap is None or tap.campus != article.campus:
            return {c: None for c in CAMPUSSES}
        price = article.price_for(article.campus)
        return {c: (price if c == article.campus else None) for c in CAMPUSSES}
    price = article.price_for(article.campus)
    has_price = price or article.price_for(article.campus, team=True)
    return {c: (price if c == article.campus and has_price else None) for c in CAMPUSSES}


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
    links = db.session.scalars(
        select(UsefulLink).order_by(UsefulLink.position, UsefulLink.id)
    ).all()
    return render_template("public/liens.html", links=links)
