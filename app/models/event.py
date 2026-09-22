from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from app.extensions import db
from app.utils import utcnow

if TYPE_CHECKING:
    from app.models.catalog import Article

GATEWAY_MARGIN = timedelta(hours=12)


class Event(db.Model):
    __tablename__ = "events"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    name: db.Mapped[str] = db.mapped_column(db.String(160))
    campus: db.Mapped[str] = db.mapped_column(db.String(10), default="brest")
    starts_at: db.Mapped[datetime] = db.mapped_column(db.DateTime)
    ends_at: db.Mapped[datetime] = db.mapped_column(db.DateTime)
    poster: db.Mapped[str | None] = db.mapped_column(db.String(255), nullable=True)
    token: db.Mapped[str] = db.mapped_column(db.String(64), unique=True, index=True)
    closed: db.Mapped[bool] = db.mapped_column(db.Boolean, default=False)
    # La passerelle encaisse toujours les articles temporaires de l'événement ;
    # ce réglage autorise en plus le catalogue standard du campus de l'événement.
    allow_standard_articles: db.Mapped[bool] = db.mapped_column(
        db.Boolean, default=True, server_default=db.true()
    )
    created_at: db.Mapped[datetime] = db.mapped_column(db.DateTime, default=utcnow)

    temporary_articles: db.Mapped[list["Article"]] = db.relationship(
        back_populates="event", cascade="all, delete-orphan"
    )

    @property
    def is_running(self):
        return (not self.closed) and self.starts_at - GATEWAY_MARGIN <= utcnow() <= self.ends_at

    @property
    def is_upcoming_or_running(self):
        return (not self.closed) and utcnow() <= self.ends_at
