from flask import Blueprint, redirect, render_template, request, url_for
from sqlalchemy import select

from app.extensions import db
from app.models import Article, Event, Note, Tap, UsefulLink
from app.services.names import public_note_authors
from app.services.settings import get_setting, int_setting
from app.services.stats import top_article_ids
from app.utils import ARTICLE_TYPES, BLAGUE_TYPE, CAMPUSSES, DEFAULT_CAMPUS, utcnow

bp = Blueprint("public", __name__)

# Taille de la carte « les plus vendus » en tête du catalogue public.
PUBLIC_TOP_COUNT = 10
# Type legacy inconnu : regroupé sous « Autres », en dernière position, pour
# ne jamais faire disparaître un article vendable du catalogue public.
PUBLIC_TYPE_FALLBACK = "autres"
PUBLIC_TYPE_FALLBACK_LABEL = "Autres"
PUBLIC_TYPE_LABELS = {**ARTICLE_TYPES, PUBLIC_TYPE_FALLBACK: PUBLIC_TYPE_FALLBACK_LABEL}
PUBLIC_TYPE_ORDER = (*ARTICLE_TYPES, PUBLIC_TYPE_FALLBACK)


@bp.route("/")
def home():
    now_events = db.session.scalars(
        select(Event)
        .where(
            Event.closed.is_(False),
            Event.ends_at >= utcnow(),
            Event.campus.in_(CAMPUSSES),
        )
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
        note_authors=public_note_authors(notes),
        homepage_text=get_setting("homepage_text"),
    )


@bp.route("/catalogue")
def catalogue():
    requested = request.args.get("campus")
    if requested is not None and requested not in CAMPUSSES:
        # Ne jamais servir le catalogue du campus par défaut sous l'URL d'un
        # campus désactivé : redirection vers l'URL canonique.
        return redirect(url_for("public.catalogue", campus=DEFAULT_CAMPUS))
    campus = requested if requested in CAMPUSSES else DEFAULT_CAMPUS
    articles = db.session.scalars(
        select(Article)
        .where(
            Article.active.is_(True),
            Article.article_type != BLAGUE_TYPE,
            Article.event_id.is_(None),
            Article.campus == campus,
        )
        .order_by(Article.name)
    ).all()
    taps = {t.number: t for t in db.session.scalars(select(Tap))}
    # Tendance : les articles les plus vendus récemment en premier, pour que les
    # étudiants trouvent vite ce qu'ils cherchent (même classement, mis en cache,
    # que la caisse). Le tri est stable : les articles sans vente récente
    # restent classés par nom, à la suite.
    ranking = top_article_ids(campus)
    rank_of = {aid: i for i, aid in enumerate(ranking)}
    articles.sort(key=lambda a: rank_of.get(a.id, len(rank_of)))
    grouped = {}
    priced = {}
    for a in articles:
        price = _public_price(a, taps)
        if price is None:
            continue
        priced[a.id] = (a, price)
        kind = a.article_type if a.article_type in ARTICLE_TYPES else PUBLIC_TYPE_FALLBACK
        grouped.setdefault(kind, []).append((a, price))
    order = {kind: index for index, kind in enumerate(PUBLIC_TYPE_ORDER)}
    ordered = sorted(grouped.items(), key=lambda kv: order.get(kv[0], len(order)))
    # Coup d'œil rapide : les plus vendus toutes catégories confondues, parmi les
    # seuls articles affichés au public (jamais d'article sans vente récente).
    top = [priced[aid] for aid in ranking if aid in priced][:PUBLIC_TOP_COUNT]
    # La colonne « Degré » n'apparaît que là où au moins un article la renseigne.
    degree_types = {
        kind for kind, entries in ordered if any(a.alcohol_degree for a, _price in entries)
    }
    return render_template(
        "public/catalogue.html",
        grouped=ordered,
        top=top,
        campus=campus,
        degree_types=degree_types,
        top_has_degree=any(a.alcohol_degree for a, _price in top),
        type_labels=PUBLIC_TYPE_LABELS,
        fallback_type_label=PUBLIC_TYPE_FALLBACK_LABEL,
    )


def _public_price(article, taps):
    """Prix public d'un article pour son campus ; None s'il ne doit pas s'afficher.

    Chaque article appartient à un unique campus (catalogues distincts) : seul
    le campus sélectionné est affiché. Un article de tireuse ne concerne que le
    campus de sa tireuse.
    """
    if article.is_tap:
        tap = taps.get(article.tap_number)
        if tap is None or tap.campus != article.campus:
            return None
        return article.price_for(article.campus)
    price = article.price_for(article.campus)
    has_price = price or article.price_for(article.campus, team=True)
    return price if has_price else None


@bp.route("/reglement")
def reglement():
    return render_template(
        "public/reglement.html",
        pdfs={key: get_setting(f"regulation_pdf_{key}") for key in CAMPUSSES},
    )


@bp.route("/liens")
def liens():
    links = db.session.scalars(
        select(UsefulLink).order_by(UsefulLink.position, UsefulLink.id)
    ).all()
    return render_template("public/liens.html", links=links)
