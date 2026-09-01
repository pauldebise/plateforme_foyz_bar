from datetime import datetime

from app.extensions import db
from app.utils import utcnow


class Transaction(db.Model):
    __tablename__ = "transactions"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    created_at: db.Mapped[datetime] = db.mapped_column(db.DateTime, default=utcnow, index=True)
    type: db.Mapped[str] = db.mapped_column(db.String(20), index=True)
    campus: db.Mapped[str] = db.mapped_column(db.String(10), index=True)
    total: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
    operator_label: db.Mapped[str] = db.mapped_column(db.String(120), default="")
    payment_method: db.Mapped[str | None] = db.mapped_column(db.String(20), nullable=True)
    event_id: db.Mapped[int | None] = db.mapped_column(db.ForeignKey("events.id", ondelete="SET NULL"), nullable=True, index=True)
    deposit_glasses: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
    deposit_user_id: db.Mapped[int | None] = db.mapped_column(db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    from_user_id: db.Mapped[int | None] = db.mapped_column(db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    to_user_id: db.Mapped[int | None] = db.mapped_column(db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    note: db.Mapped[str | None] = db.mapped_column(db.String(255), nullable=True)
    cancelled: db.Mapped[bool] = db.mapped_column(db.Boolean, default=False, index=True)
    cancelled_at: db.Mapped[datetime | None] = db.mapped_column(db.DateTime, nullable=True)

    lines: db.Mapped[list["TransactionLine"]] = db.relationship(
        back_populates="transaction", cascade="all, delete-orphan", lazy="selectin"
    )
    contributions: db.Mapped[list["Contribution"]] = db.relationship(
        back_populates="transaction", cascade="all, delete-orphan", lazy="selectin"
    )
    event: db.Mapped["Event"] = db.relationship()


class TransactionLine(db.Model):
    __tablename__ = "transaction_lines"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    transaction_id: db.Mapped[int] = db.mapped_column(db.ForeignKey("transactions.id", ondelete="CASCADE"), index=True)
    article_id: db.Mapped[int | None] = db.mapped_column(db.ForeignKey("articles.id", ondelete="SET NULL"), nullable=True)
    article_name: db.Mapped[str] = db.mapped_column(db.String(200))
    article_type: db.Mapped[str] = db.mapped_column(db.String(20), default="biere")
    quantity: db.Mapped[int] = db.mapped_column(db.Integer, default=1)
    unit_price: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
    line_total: db.Mapped[int] = db.mapped_column(db.Integer, default=0)

    transaction: db.Mapped["Transaction"] = db.relationship(back_populates="lines")


class Contribution(db.Model):
    __tablename__ = "contributions"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    transaction_id: db.Mapped[int] = db.mapped_column(db.ForeignKey("transactions.id", ondelete="CASCADE"), index=True)
    user_id: db.Mapped[int | None] = db.mapped_column(db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    campus: db.Mapped[str] = db.mapped_column(db.String(10), default="brest")
    amount: db.Mapped[int] = db.mapped_column(db.Integer, default=0)
    balance_after: db.Mapped[int] = db.mapped_column(db.Integer, default=0)

    transaction: db.Mapped["Transaction"] = db.relationship(back_populates="contributions")
    user: db.Mapped["User"] = db.relationship()
