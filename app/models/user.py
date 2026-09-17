from datetime import datetime

from app.extensions import db
from app.utils import utcnow


class User(db.Model):
    __tablename__ = "users"

    id: db.Mapped[int] = db.mapped_column(db.Integer, primary_key=True)
    # Identifiant de connexion, exclusivement : minuscule sans accents,
    # séparateurs ramenés à des points (cf. app.utils.slug_username).
    username: db.Mapped[str] = db.mapped_column(db.String(64), unique=True, index=True)
    # Nom réel complet, utilisé pour l'affichage via display_name.
    name: db.Mapped[str] = db.mapped_column(db.String(255), index=True)
    # Surnom d'usage (pseudo de l'ancienne plateforme), facultatif et non
    # unique : intégré à l'affichage et utilisable comme libellé de recherche.
    nickname: db.Mapped[str | None] = db.mapped_column(db.String(255), nullable=True, default=None)
    promotion: db.Mapped[int | None] = db.mapped_column(db.Integer, nullable=True)
    password_hash: db.Mapped[str | None] = db.mapped_column(db.String(255), nullable=True)
    # Mot de passe hérité de l'ancienne plateforme (bcrypt/md5/texte brut),
    # importé par la migration : vérifié à la connexion puis converti en
    # password_hash (format werkzeug) et vidé.
    legacy_password: db.Mapped[str | None] = db.mapped_column(db.String(255), nullable=True, default=None)
    team_status: db.Mapped[str | None] = db.mapped_column(db.String(10), nullable=True, default=None)
    team_campus: db.Mapped[str | None] = db.mapped_column(db.String(10), nullable=True, default=None)
    blacklist: db.Mapped[bool] = db.mapped_column(db.Boolean, default=False)
    blacklist_alcohol: db.Mapped[bool] = db.mapped_column(db.Boolean, default=False)
    blacklist_reason: db.Mapped[str | None] = db.mapped_column(db.String(255), nullable=True, default=None)
    # Compte désactivé côté ancienne base : conservé désactivé à la migration,
    # la connexion est refusée (distinct d'une blacklist « comportement »).
    disabled: db.Mapped[bool] = db.mapped_column(db.Boolean, default=False,
                                                 server_default=db.false())
    # Trombinoscope public : membre affiché (mandat) ou masqué, rôle affiché
    # sous le nom, et photo (nom de fichier dans uploads/, facultative).
    trombinoscope_visible: db.Mapped[bool] = db.mapped_column(
        db.Boolean, default=True, server_default=db.true()
    )
    trombinoscope_role: db.Mapped[str | None] = db.mapped_column(
        db.String(80), nullable=True, default=None
    )
    photo: db.Mapped[str | None] = db.mapped_column(db.String(255), nullable=True, default=None)
    created_at: db.Mapped[datetime] = db.mapped_column(db.DateTime, default=utcnow)

    wallets: db.Mapped[list["Wallet"]] = db.relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_team(self):
        return self.team_status in ("mandat", "ancien")

    @property
    def display_name(self):
        """Nom long complet affiché partout (comptes, caisse, stats, historique) :
        le nom réel, enrichi du surnom s'il existe (« Paul Debise (chips) »)."""
        return f"{self.name} ({self.nickname})" if self.nickname else self.name

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
        return self.user.display_name if self.user else "?"
