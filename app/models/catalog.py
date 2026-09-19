from datetime import datetime
from typing import TYPE_CHECKING

from app.extensions import db
from app.utils import utcnow

if TYPE_CHECKING:
    from app.models.event import Event


class Article(db.Model):
    __tablename__ = "articles"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    name: db.Mapped[str] = db.mapped_column(db.String(255))
    article_type: db.Mapped[str] = db.mapped_column(db.String(20), default="biere", index=True)
    volume_cl: db.Mapped[int | None] = db.mapped_column(db.Integer, nullable=True)
    # Chaque article appartient à un unique campus : les catalogues Brest et
    # Paris sont distincts (un article commun aux deux bases historiques doit
    # être enregistré deux fois, une par campus).
    campus: db.Mapped[str] = db.mapped_column(db.String(10), default="brest", index=True)
    price_std: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
    price_team: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
    is_alcohol: db.Mapped[bool] = db.mapped_column(db.Boolean, default=False)
    is_tap: db.Mapped[bool] = db.mapped_column(db.Boolean, default=False, index=True)
    tap_number: db.Mapped[int | None] = db.mapped_column(db.Integer, nullable=True)
    keg_id: db.Mapped[int | None] = db.mapped_column(
        db.ForeignKey("kegs.id", ondelete="SET NULL"), nullable=True
    )
    event_id: db.Mapped[int | None] = db.mapped_column(
        db.ForeignKey("events.id", ondelete="CASCADE"), nullable=True, index=True
    )
    active: db.Mapped[bool] = db.mapped_column(db.Boolean, default=True)
    created_at: db.Mapped[datetime] = db.mapped_column(db.DateTime, default=utcnow)

    event: db.Mapped["Event"] = db.relationship(back_populates="temporary_articles")
    keg: db.Mapped["Keg"] = db.relationship(backref="articles")

    def price_for(self, campus, team=False):
        """Prix public/équipe : 0 dès que le campus demandé n'est pas celui de
        l'article (un article brestois n'est ni visible ni vendable à Paris)."""
        if campus != self.campus:
            return 0
        return self.price_team if team else self.price_std


class Keg(db.Model):
    __tablename__ = "kegs"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    name: db.Mapped[str] = db.mapped_column(db.String(255))
    alcohol_degree: db.Mapped[float] = db.mapped_column(db.Float, default=0.0)
    volume_l: db.Mapped[float] = db.mapped_column(db.Float, default=30.0)
    remaining_l: db.Mapped[float] = db.mapped_column(db.Float, default=30.0)
    active: db.Mapped[bool] = db.mapped_column(db.Boolean, default=True)
    created_at: db.Mapped[datetime] = db.mapped_column(db.DateTime, default=utcnow)

    prices: db.Mapped[list["KegPrice"]] = db.relationship(
        back_populates="keg", cascade="all, delete-orphan", lazy="selectin"
    )

    def price_row(self, campus):
        for p in self.prices:
            if p.campus == campus:
                return p
        row = KegPrice(keg=self, campus=campus)
        db.session.add(row)
        db.session.flush()
        return row


class KegPrice(db.Model):
    __tablename__ = "keg_prices"
    __table_args__ = (db.UniqueConstraint("keg_id", "campus", name="uq_kegprice_keg_campus"),)

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    keg_id: db.Mapped[int] = db.mapped_column(db.ForeignKey("kegs.id", ondelete="CASCADE"))
    campus: db.Mapped[str] = db.mapped_column(db.String(10))
    price_half_std: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
    price_pint_std: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
    price_pot_std: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
    price_half_team: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
    price_pint_team: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
    price_pot_team: db.Mapped[int] = db.mapped_column(db.Integer, default=0)

    keg: db.Mapped["Keg"] = db.relationship(back_populates="prices")


class Tap(db.Model):
    __tablename__ = "taps"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    number: db.Mapped[int] = db.mapped_column(db.Integer, unique=True)
    name: db.Mapped[str | None] = db.mapped_column(db.String(255), nullable=True)
    campus: db.Mapped[str] = db.mapped_column(db.String(10), default="brest")
    keg_id: db.Mapped[int | None] = db.mapped_column(
        db.ForeignKey("kegs.id", ondelete="SET NULL"), nullable=True
    )

    keg: db.Mapped["Keg"] = db.relationship(backref="taps")

    @property
    def display_name(self):
        return self.name or f"Tireuse {self.number}"
