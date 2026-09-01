from datetime import datetime

from app.extensions import db
from app.utils import utcnow


class Note(db.Model):
    __tablename__ = "notes"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    content: db.Mapped[str] = db.mapped_column(db.Text)
    is_public: db.Mapped[bool] = db.mapped_column(db.Boolean, default=False, index=True)
    author_id: db.Mapped[int | None] = db.mapped_column(db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    author_name: db.Mapped[str] = db.mapped_column(db.String(120), default="")
    created_at: db.Mapped[datetime] = db.mapped_column(db.DateTime, default=utcnow, index=True)
    updated_at: db.Mapped[datetime] = db.mapped_column(db.DateTime, default=utcnow, onupdate=utcnow)
