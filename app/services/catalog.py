from sqlalchemy import select

from app.extensions import db
from app.models import Article, Keg, Note, Tap
from app.utils import TAP_SIZES


def assign_keg(tap, keg):
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
        tap_label = tap.name or f"tireuse {tap.number}"
        db.session.add(
            Article(
                name=f"{label} de {tap_label} ({keg.name})",
                article_type="biere",
                volume_cl=vol,
                price_std_brest=std,
                price_std_paris=std,
                price_team_brest=team,
                price_team_paris=team,
                is_alcohol=True,
                is_tap=True,
                tap_number=tap.number,
                keg_id=keg.id,
                active=keg.remaining_l > 0.01,
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


def refresh_tap_articles(keg):
    for tap in db.session.scalars(select(Tap).where(Tap.keg_id == keg.id)):
        assign_keg(tap, keg)


def trim_notes(is_public, limit):
    notes = db.session.scalars(
        select(Note).where(Note.is_public.is_(is_public)).order_by(Note.created_at.desc())
    ).all()
    for old in notes[limit:]:
        db.session.delete(old)
    db.session.commit()
