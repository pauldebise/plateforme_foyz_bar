from datetime import datetime

from app.extensions import db
from app.utils import utcnow


class User(db.Model):
    __tablename__ = "users"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    name: db.Mapped[str] = db.mapped_column(db.String(80), unique=True, index=True)
    promotion: db.Mapped[int | None] = db.mapped_column(db.Integer, nullable=True)
    password_hash: db.Mapped[str | None] = db.mapped_column(db.String(255), nullable=True)
    team_status: db.Mapped[str | None] = db.mapped_column(db.String(10), nullable=True, default=None)
    team_campus: db.Mapped[str | None] = db.mapped_column(db.String(10), nullable=True, default=None)
    blacklist: db.Mapped[bool] = db.mapped_column(db.Boolean, default=False)
    blacklist_alcohol: db.Mapped[bool] = db.mapped_column(db.Boolean, default=False)
    blacklist_reason: db.Mapped[str | None] = db.mapped_column(db.String(255), nullable=True, default=None)
    created_at: db.Mapped[datetime] = db.mapped_column(db.DateTime, default=utcnow)

    wallets: db.Mapped[list["Wallet"]] = db.relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_team(self):
        return self.team_status in ("mandat", "ancien")

    def wallet(self, campus):
        for w in self.wallets:
            if w.campus == campus:
                return w
        w = Wallet(user=self, campus=campus, balance=0)
        db.session.add(w)
        db.session.flush()
        return w


class Wallet(db.Model):
    __tablename__ = "wallets"
    __table_args__ = (db.UniqueConstraint("user_id", "campus", name="uq_wallet_user_campus"),)

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    user_id: db.Mapped[int] = db.mapped_column(db.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    campus: db.Mapped[str] = db.mapped_column(db.String(10))
    balance: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
    glasses_outstanding: db.Mapped[int] = db.mapped_column(db.Integer, default=0)

    user: db.Mapped["User"] = db.relationship(back_populates="wallets")

    @property
    def owner_name(self):
        return self.user.name if self.user else "?"
