from datetime import datetime
from typing import TYPE_CHECKING

from app.extensions import db
from app.utils import utcnow

if TYPE_CHECKING:
    from app.models.catalog import Article, Keg


class ApiToken(db.Model):
    """Jeton d'accès à l'API externe, géré depuis /admin/api.

    Seule l'empreinte SHA-256 est conservée : le jeton en clair n'est affiché
    qu'une fois, au moment de sa création. Le préfixe permet de l'identifier
    dans les journaux et l'interface sans jamais révéler le secret.
    """

    __tablename__ = "api_tokens"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    label: db.Mapped[str] = db.mapped_column(db.String(160))
    token_hash: db.Mapped[str] = db.mapped_column(db.String(64), unique=True, index=True)
    token_prefix: db.Mapped[str] = db.mapped_column(db.String(16))
    campus: db.Mapped[str] = db.mapped_column(db.String(10), index=True)
    can_read_consumption: db.Mapped[bool] = db.mapped_column(db.Boolean, default=True)
    can_write_prices: db.Mapped[bool] = db.mapped_column(db.Boolean, default=True)
    # Prix équipe : droits distincts, désactivés par défaut. Les jetons
    # existants conservent leurs droits actuels après la migration.
    can_write_team_prices: db.Mapped[bool] = db.mapped_column(db.Boolean, default=False)
    can_read_team_prices: db.Mapped[bool] = db.mapped_column(db.Boolean, default=False)
    # Fenêtre de lecture figée à la création (mais modifiable depuis l'onglet).
    history_from: db.Mapped[datetime] = db.mapped_column(db.DateTime)
    history_to: db.Mapped[datetime] = db.mapped_column(db.DateTime)
    expires_at: db.Mapped[datetime] = db.mapped_column(db.DateTime, index=True)
    created_at: db.Mapped[datetime] = db.mapped_column(db.DateTime, default=utcnow)
    updated_at: db.Mapped[datetime] = db.mapped_column(db.DateTime, default=utcnow, onupdate=utcnow)
    revoked_at: db.Mapped[datetime | None] = db.mapped_column(
        db.DateTime, nullable=True, index=True
    )
    last_used_at: db.Mapped[datetime | None] = db.mapped_column(db.DateTime, nullable=True)
    # Dernier appel, aussi utilisé pour la limite d'une requête par seconde
    # (mise à jour atomique partagée entre les workers Gunicorn).
    last_request_at: db.Mapped[datetime | None] = db.mapped_column(db.DateTime, nullable=True)
    created_by_id: db.Mapped[int | None] = db.mapped_column(
        db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    article_links: db.Mapped[list["ApiTokenArticle"]] = db.relationship(
        back_populates="token", cascade="all, delete-orphan", lazy="selectin"
    )
    keg_links: db.Mapped[list["ApiTokenKeg"]] = db.relationship(
        back_populates="token", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    @property
    def is_expired(self):
        return utcnow() > self.expires_at

    @property
    def is_active(self):
        return not self.is_revoked and not self.is_expired

    @property
    def scope_labels(self):
        labels = []
        if self.can_read_consumption:
            labels.append("consommation:lecture")
        if self.can_write_prices:
            labels.append("prix:ecriture")
        if self.can_write_team_prices:
            labels.append("prix_equipe:ecriture")
        if self.can_read_team_prices:
            labels.append("prix_equipe:lecture")
        return labels


class ApiTokenArticle(db.Model):
    __tablename__ = "api_token_articles"
    __table_args__ = (db.UniqueConstraint("token_id", "article_id", name="uq_api_token_article"),)

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    token_id: db.Mapped[int] = db.mapped_column(
        db.ForeignKey("api_tokens.id", ondelete="CASCADE"), index=True
    )
    article_id: db.Mapped[int] = db.mapped_column(
        db.ForeignKey("articles.id", ondelete="CASCADE"), index=True
    )

    token: db.Mapped["ApiToken"] = db.relationship(back_populates="article_links")
    article: db.Mapped["Article"] = db.relationship()


class ApiTokenKeg(db.Model):
    __tablename__ = "api_token_kegs"
    __table_args__ = (db.UniqueConstraint("token_id", "keg_id", name="uq_api_token_keg"),)

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    token_id: db.Mapped[int] = db.mapped_column(
        db.ForeignKey("api_tokens.id", ondelete="CASCADE"), index=True
    )
    keg_id: db.Mapped[int] = db.mapped_column(
        db.ForeignKey("kegs.id", ondelete="CASCADE"), index=True
    )

    token: db.Mapped["ApiToken"] = db.relationship(back_populates="keg_links")
    keg: db.Mapped["Keg"] = db.relationship()
