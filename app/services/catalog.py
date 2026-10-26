from sqlalchemy import select

from app.extensions import db
from app.models import Article, Note, Tap
from app.utils import TAP_SIZES, clamp_text


def _tap_article_name(label, tap, keg):
    """Nom d'un article de tireuse : taille, tireuse, fût et degré d'alcool.

    Le degré est omis tant qu'il n'est pas renseigné (0 °) : afficher
    « 0.0° » laisserait croire à une bière sans alcool."""
    tap_label = tap.name or f"tireuse {tap.number}"
    degree = keg.alcohol_degree or 0.0
    keg_label = f"{keg.name} - {degree:.1f}°" if degree > 0 else keg.name
    return clamp_text(f"{label} de {tap_label} ({keg_label})", 255)


def assign_keg(tap, keg):
    """Monte un fût sur une tireuse et régénère ses trois articles.

    Les articles déjà présents sur la tireuse sont désactivés : seuls les
    nouveaux articles, créés avec le nom et les prix actuels du fût, restent
    proposés à la caisse.
    """
    tap.keg_id = keg.id
    for a in db.session.scalars(
        select(Article).where(Article.tap_number == tap.number, Article.is_tap.is_(True))
    ):
        a.active = False
    row = keg.price_row(tap.campus)
    prices = {
        "demi": (row.price_half_std, row.price_half_team),
        "pinte": (row.price_pint_std, row.price_pint_team),
        "pot": (row.price_pot_std, row.price_pot_team),
    }
    for key, (label, vol) in TAP_SIZES.items():
        std, team = prices[key]
        db.session.add(
            Article(
                name=_tap_article_name(label, tap, keg),
                article_type="biere",
                volume_cl=vol,
                campus=tap.campus,
                price_std=std,
                price_team=team,
                is_alcohol=True,
                is_tap=True,
                tap_number=tap.number,
                keg_id=keg.id,
                active=True,
            )
        )
    db.session.commit()


def detach_keg(tap):
    tap.keg_id = None
    for a in db.session.scalars(
        select(Article).where(Article.tap_number == tap.number, Article.is_tap.is_(True))
    ):
        a.active = False
    db.session.commit()


def delete_tap(tap):
    for a in db.session.scalars(
        select(Article).where(Article.tap_number == tap.number, Article.is_tap.is_(True))
    ):
        a.active = False
    db.session.delete(tap)
    db.session.commit()


def refresh_tap_articles(keg, campus=None):
    """Régénère les articles des tireuses portant ce fût.

    Appelée après chaque modification du fût pour que la caisse voie
    immédiatement les nouvelles valeurs. Un même fût peut être monté sur
    plusieurs tireuses : celles du campus demandé sont régénérées ; sans
    `campus`, toutes le sont.
    """
    stmt = select(Tap).where(Tap.keg_id == keg.id)
    if campus:
        stmt = stmt.where(Tap.campus == campus)
    for tap in db.session.scalars(stmt):
        assign_keg(tap, keg)


def trim_notes(is_public, limit):
    notes = db.session.scalars(
        select(Note).where(Note.is_public.is_(is_public)).order_by(Note.created_at.desc())
    ).all()
    for old in notes[limit:]:
        db.session.delete(old)
    db.session.commit()
