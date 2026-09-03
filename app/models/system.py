from datetime import datetime

from app.extensions import db
from app.utils import utcnow


class Setting(db.Model):
    __tablename__ = "settings"

    key: db.Mapped[str] = db.mapped_column(db.String(80), primary_key=True)
    value: db.Mapped[str] = db.mapped_column(db.Text, default="")


class LoginLog(db.Model):
    __tablename__ = "login_logs"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    user_id: db.Mapped[int | None] = db.mapped_column(db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    name: db.Mapped[str] = db.mapped_column(db.String(120), default="")
    campus: db.Mapped[str] = db.mapped_column(db.String(10), default="")
    ip: db.Mapped[str] = db.mapped_column(db.String(64), default="")
    success: db.Mapped[bool] = db.mapped_column(db.Boolean, default=True)
    created_at: db.Mapped[datetime] = db.mapped_column(db.DateTime, default=utcnow, index=True)


class UsefulLink(db.Model):
    __tablename__ = "useful_links"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    label: db.Mapped[str] = db.mapped_column(db.String(160))
    url: db.Mapped[str] = db.mapped_column(db.String(500))
    position: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
