import time
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Article,
    Contribution,
    Keg,
    Tap,
    Transaction,
    TransactionLine,
    User,
    Wallet,
)
from app.services import settings as S
from app.utils import (
    ALCOHOL_TYPES,
    PAYMENT_METHODS,
    utcnow,
)


class OperationError(Exception):
    def __init__(self, code, message, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


def _int_or_invalid(value, message="Valeur numérique invalide."):
    """Conversion défensive : une entrée non numérique est un refus métier,
    jamais une erreur serveur (500)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise OperationError("invalid", message) from None


def _get_wallet(user, campus):
    return user.wallet(campus)


def wallet_view(user, campus):
    """Portefeuille existant sans le créer (consultation d'un campus)."""
    return next((w for w in user.wallets if w.campus == campus), None)


def _lock_wallets(campus, users):
    """Verrouille (SELECT … FOR UPDATE) les portefeuilles des utilisateurs.

    Les verrous sont pris dans l'ordre des identifiants pour éviter les
    interblocages entre workers Gunicorn. Sur SQLite, FOR UPDATE est ignoré :
    les soldes sont de toute façon modifiés de façon *relative*
    (``Wallet.balance + :delta``) et non réécrits depuis une valeur lue, ce
    qui supprime les lost updates même sans verrou.
    """
    for u in users:
        _get_wallet(u, campus)
    db.session.flush()
    if not users:
        return {}
    ids = sorted({u.id for u in users})
    rows = db.session.scalars(
        select(Wallet)
        .where(Wallet.campus == campus, Wallet.user_id.in_(ids))
        .order_by(Wallet.user_id)
        .with_for_update()
    ).all()
    by_user = {w.user_id: w for w in rows}
    return {u.id: by_user[u.id] for u in users}


def _lock_kegs(keg_ids):
    """Verrouille les fûts concernés, dans un ordre stable."""
    ids = sorted({kid for kid in keg_ids if kid})
    if not ids:
        return {}
    rows = db.session.scalars(
        select(Keg).where(Keg.id.in_(ids)).order_by(Keg.id).with_for_update()
    ).all()
    return {k.id: k for k in rows}


def _existing_by_key(idempotency_key):
    if not idempotency_key:
        return None
    return db.session.scalars(
        select(Transaction).where(Transaction.idempotency_key == idempotency_key)
    ).first()


def _split_shares(total, n):
    """Répartit ``total`` (centimes) en ``n`` parts entières.

    Les centimes indivisibles sont distribués un par un aux premiers comptes,
    de sorte que la somme des parts vaille exactement ``total`` et que l'écart
    entre deux parts n'excède jamais un centime.
    """
    base, remainder = divmod(total, n)
    shares = [base] * n
    for i in range(remainder):
        shares[i] += 1
    return shares


def _unique_users(users, message):
    """Déduplique une sélection d'utilisateurs en conservant l'ordre choisi."""
    unique = []
    seen = set()
    for u in users or []:
        if u is None or u.id in seen:
            continue
        seen.add(u.id)
        unique.append(u)
    if not unique:
        raise OperationError("invalid", message)
    return unique


# Classement par activité récente : mis en cache quelques secondes par campus
# car il ne change qu'à un encaissement, alors que la recherche le recalcule à
# chaque frappe (fenêtre glissante de 45 jours sur `contributions`).
_ACTIVITY_TTL_SECONDS = 30
_activity_cache = {}


def _invalidate_activity_cache():
    _activity_cache.clear()


def _recent_activity(campus=None, days=45):
    """Achats récents par utilisateur : {user_id: (nombre, dernier achat)}."""
    key = (campus, days)
    now = time.monotonic()
    cached = _activity_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    since = utcnow() - timedelta(days=days)
    # Les transactions récentes sont sélectionnées à part : le planificateur
    # s'appuie alors sur l'index `contributions.transaction_id` au lieu de
    # balayer toute la table des contributions (des centaines de milliers de
    # lignes, dont l'immense majorité est hors fenêtre).
    recent = select(Transaction.id).where(
        Transaction.type == "achat",
        Transaction.cancelled.is_(False),
        Transaction.created_at >= since,
    )
    conditions = [
        Contribution.user_id.isnot(None),
        Contribution.transaction_id.in_(recent),
    ]
    if campus:
        conditions.append(Contribution.campus == campus)
    stmt = (
        select(
            Contribution.user_id,
            func.count(Contribution.id),
            func.max(Transaction.created_at),
        )
        .join(Transaction, Transaction.id == Contribution.transaction_id)
        .where(*conditions)
        .group_by(Contribution.user_id)
    )
    ranking = {uid: (count, last_at) for uid, count, last_at in db.session.execute(stmt).all()}
    _activity_cache[key] = (now + _ACTIVITY_TTL_SECONDS, ranking)
    return ranking


def search_students(query, campus=None, limit=15):
    q = (query or "").strip()
    ranking = _recent_activity(campus)
    # En tête : comptes les plus actifs récemment ; ensuite alphabétique.
    # La recherche porte sur le nom réel, le surnom et l'identifiant.
    stmt = select(User.id, User.name, User.nickname)
    if q:
        # Chaque mot de la requête doit apparaître (dans n'importe quel ordre)
        # dans le nom, le surnom ou l'identifiant : « prénom nom » retrouve
        # donc un compte enregistré « nom prénom ».
        tokens = [t for t in q.split() if t]
        stmt = stmt.where(
            and_(
                *[
                    or_(
                        User.name.ilike(f"%{t}%"),
                        User.nickname.ilike(f"%{t}%"),
                        User.username.ilike(f"%{t}%"),
                    )
                    for t in tokens
                ]
            )
        )
    candidates = db.session.execute(stmt).all()

    def rank_key(row):
        count, last_at = ranking.get(row.id, (0, None))
        return (
            -count,
            -(last_at.timestamp() if last_at else 0.0),
            (row.name or "").casefold(),
        )

    # Tri du classement sur les seules colonnes utiles (id/nom), puis chargement
    # des portefeuilles des `limit` retenus uniquement.
    candidates.sort(key=rank_key)
    top_ids = [row.id for row in candidates[:limit]]
    users = {u.id: u for u in db.session.scalars(select(User).where(User.id.in_(top_ids)))}
    results = []
    for uid in top_ids:
        u = users.get(uid)
        if u is None:
            continue
        w = wallet_view(u, campus) if campus else None
        results.append(
            {
                "id": u.id,
                "name": u.display_name,
                "username": u.username,
                "promotion": u.promotion,
                "blacklist": u.blacklist,
                "blacklist_alcohol": u.blacklist_alcohol,
                "is_team": u.is_team,
                "balance": w.balance if w else 0,
                "glasses": w.glasses_outstanding if w else 0,
            }
        )
    return results


def _resolve_items(items, event_id=None, campus=None):
    if not items:
        raise OperationError("invalid", "Aucun article sélectionné.")
    merged = {}
    for it in items:
        if not isinstance(it, dict):
            raise OperationError("invalid", "Commande invalide.")
        try:
            aid = int(it.get("article_id"))
            qty = int(it.get("quantity", 0))
        except (TypeError, ValueError):
            raise OperationError("invalid", "Commande invalide.") from None
        if qty <= 0:
            continue
        merged[aid] = merged.get(aid, 0) + qty
    if not merged:
        raise OperationError("invalid", "Aucun article sélectionné.")

    stmt = select(Article).where(Article.id.in_(merged.keys()), Article.active.is_(True))
    articles = {a.id: a for a in db.session.scalars(stmt)}
    lines = []
    for aid, qty in merged.items():
        a = articles.get(aid)
        # Chaque campus n'encaisse que ses propres articles : un identifiant
        # d'article d'un autre campus (ou inconnu/inactif) est refusé.
        if a is None or a.campus != campus:
            raise OperationError("invalid", "Article indisponible.")
        if a.event_id != event_id:
            # hors d'un événement, seuls les articles standard sont vendables ;
            # en contexte événement (passerelle), le catalogue standard du
            # campus reste disponible en plus des articles de l'événement
            standard_ok = (
                event_id is not None
                and a.event_id is None
                and campus in ("brest", "paris")
                and a.price_for(campus) > 0
            )
            if not standard_ok:
                raise OperationError("invalid", "Article indisponible.")
        lines.append((a, qty))
    return lines


def create_purchase(
    *,
    operator_label,
    campus,
    items,
    contributor_ids=None,
    deposit_glasses=0,
    direct=False,
    payment_method=None,
    event_id=None,
    admin_password=None,
    idempotency_key=None,
):
    # Rejeu d'un encaissement déjà traité : on renvoie la transaction existante
    # plutôt que d'en créer une seconde (double soumission, retentative réseau).
    existing = _existing_by_key(idempotency_key)
    if existing is not None:
        return existing

    campus = (campus or "").lower()
    if campus not in ("brest", "paris"):
        raise OperationError("invalid", "Campus invalide.")
    if direct:
        if payment_method not in PAYMENT_METHODS:
            raise OperationError("invalid", "Moyen de paiement invalide.")
        contributor_ids = []
        deposit_glasses = 0
    else:
        contributor_ids = [
            _int_or_invalid(x, "Étudiant introuvable.") for x in (contributor_ids or [])
        ]
        if not contributor_ids:
            raise OperationError("invalid", "Aucun étudiant sélectionné.")

    lines = _resolve_items(items, event_id=event_id, campus=campus)
    users = []
    if not direct:
        seen = set()
        for uid in contributor_ids:
            if uid in seen:
                continue
            seen.add(uid)
            u = db.session.get(User, uid)
            if u is None:
                raise OperationError("invalid", "Étudiant introuvable.")
            users.append(u)

        for u in users:
            if u.blacklist:
                raise OperationError(
                    "blacklist",
                    f"{u.display_name} est blacklisté : la transaction est impossible.",
                )
    has_alcohol = any(a.is_alcohol for a, _ in lines) or any(
        a.article_type in ALCOHOL_TYPES for a, _ in lines
    )
    if not direct and has_alcohol:
        for u in users:
            if u.blacklist_alcohol:
                raise OperationError(
                    "alcohol",
                    f"{u.display_name} est blacklist alcool : commande avec alcool refusée.",
                )

    all_team = (not direct) and users and all(u.is_team for u in users)
    cart = []
    keg_volumes = defaultdict(float)
    article_total = 0
    for a, qty in lines:
        unit = a.price_for(campus, team=all_team)
        line_total = unit * qty
        article_total += line_total
        cart.append({"article": a, "quantity": qty, "unit_price": unit, "line_total": line_total})
        if a.is_tap and a.keg_id:
            keg_volumes[a.keg_id] += (a.volume_cl or 0) * qty / 100.0

    glasses = 0
    deposit_total = 0
    primary = users[0] if users else None
    if not direct and deposit_glasses:
        glasses = max(0, _int_or_invalid(deposit_glasses, "Nombre de consignes invalide."))
        if glasses > 0:
            if not S.deposit_enabled():
                raise OperationError("invalid", "La consigne est désactivée.")
            deposit_total = glasses * S.deposit_value()

    total = article_total + deposit_total
    if total <= 0:
        raise OperationError("invalid", "Montant nul.")

    n = len(users) if users else 1
    shares = _split_shares(total, n)

    # Verrouillage cohérent : portefeuilles puis fûts, ordonnés par identifiant
    # (les deux ensembles sont verrouillés dans le même ordre partout).
    wallets = _lock_wallets(campus, users) if not direct else {}
    kegs = _lock_kegs(keg_volumes.keys())

    negative = []
    if not direct:
        for u, share in zip(users, shares, strict=True):
            w = wallets[u.id]
            new_balance = w.balance - share
            if new_balance < 0:
                negative.append((u, new_balance))
        for u, new_balance in negative:
            if -new_balance > S.overdraft_limit():
                raise OperationError(
                    "overdraft_limit",
                    f"Découvert maximum dépassé pour {u.display_name} : transaction refusée.",
                )
        if negative and not S.check_admin_password(admin_password, campus):
            raise OperationError(
                "admin_password_required",
                "Un étudiant passera en négatif : mot de passe administrateur requis.",
                {"negative_users": [u.display_name for u, _ in negative]},
            )

    ttype = "direct" if direct else "achat"
    t = Transaction(
        type=ttype,
        campus=campus,
        total=total,
        operator_label=operator_label or "",
        payment_method=payment_method if direct else None,
        event_id=event_id,
        deposit_glasses=glasses,
        deposit_user_id=primary.id if primary and glasses else None,
        idempotency_key=idempotency_key or None,
    )
    db.session.add(t)
    try:
        db.session.flush()
    except IntegrityError:
        # Rejeu concurrent du même jeton : la transaction gagnante a été
        # insérée (le flush attend sa validation), on la renvoie telle quelle.
        db.session.rollback()
        existing = _existing_by_key(idempotency_key)
        if existing is not None:
            return existing
        raise
    for c in cart:
        db.session.add(
            TransactionLine(
                transaction_id=t.id,
                article_id=c["article"].id,
                article_name=c["article"].name,
                article_type=c["article"].article_type,
                quantity=c["quantity"],
                unit_price=c["unit_price"],
                line_total=c["line_total"],
            )
        )

    # Mouvements relatifs (UPDATE … SET balance = balance ± :x) : la valeur de
    # référence vient de la base, jamais d'une lecture préalable, donc deux
    # workers concurrents ne peuvent pas écraser mutuellement leur écriture.
    if not direct:
        for u, share in zip(users, shares, strict=True):
            wallets[u.id].balance = Wallet.balance - share
        if glasses and primary is not None:
            wp = wallets[primary.id]
            wp.glasses_outstanding = Wallet.glasses_outstanding + glasses
    for keg_id, volume_l in keg_volumes.items():
        keg = kegs.get(keg_id)
        if keg is not None:
            keg.remaining_l = Keg.remaining_l - volume_l
    db.session.flush()

    if not direct:
        # Contrôle post-écriture : sur SQLite (verrous ignorés) c'est ce
        # contrôle qui garantit que le découvert maximum n'est jamais dépassé.
        limit = S.overdraft_limit()
        for u in users:
            if wallets[u.id].balance < -limit:
                db.session.rollback()
                raise OperationError(
                    "overdraft_limit",
                    f"Découvert maximum dépassé pour {u.display_name} : transaction refusée.",
                )
        for u, share in zip(users, shares, strict=True):
            w = wallets[u.id]
            db.session.add(
                Contribution(
                    transaction_id=t.id,
                    user_id=u.id,
                    campus=campus,
                    amount=-share,
                    balance_after=w.balance,
                )
            )
    else:
        db.session.add(
            Contribution(
                transaction_id=t.id,
                user_id=None,
                campus=campus,
                amount=total,
                balance_after=0,
            )
        )

    for keg in kegs.values():
        if keg.remaining_l <= 0.01:
            keg.remaining_l = 0.0
            _drain_tap(keg)

    try:
        db.session.commit()
    except IntegrityError:
        # Course entre deux rejeux du même jeton d'idempotence : on renvoie la
        # transaction gagnante plutôt que de propager l'erreur d'unicité.
        db.session.rollback()
        existing = _existing_by_key(idempotency_key)
        if existing is not None:
            return existing
        raise
    _invalidate_activity_cache()
    return t


def _drain_tap(keg):
    for tap in db.session.scalars(select(Tap).where(Tap.keg_id == keg.id)):
        tap.keg_id = None
    for article in db.session.scalars(select(Article).where(Article.keg_id == keg.id)):
        article.active = False


def _reactivate_keg_articles(keg, tap_numbers):
    """Rétablit le catalogue pression d'un fût après annulation d'une vente.

    Les articles de tireuse sont réactivés (sauf s'ils sont remplacés par un
    autre fût sur la même tireuse) et les tireuses laissées vides par le
    vidage sont rebranchées sur ce fût.
    """
    active_numbers = {
        a.tap_number
        for a in db.session.scalars(
            select(Article).where(
                Article.keg_id == keg.id, Article.is_tap.is_(True), Article.active.is_(True)
            )
        )
    }
    for article in db.session.scalars(
        select(Article).where(
            Article.keg_id == keg.id, Article.is_tap.is_(True), Article.active.is_(False)
        )
    ):
        if article.tap_number in active_numbers:
            continue
        article.active = True
        active_numbers.add(article.tap_number)
    if tap_numbers:
        for tap in db.session.scalars(select(Tap).where(Tap.number.in_(tap_numbers))):
            if tap.keg_id is None:
                tap.keg_id = keg.id


def return_glasses(*, operator_label, campus, user, count):
    count = _int_or_invalid(count or 0, "Nombre de verres invalide.")
    if count <= 0:
        raise OperationError("invalid", "Nombre de verres invalide.")
    w = _lock_wallets(campus, [user])[user.id]
    if w.glasses_outstanding < count:
        raise OperationError(
            "invalid", f"{user.display_name} n'a que {w.glasses_outstanding} verre(s) consigné(s)."
        )
    credit = count * S.deposit_value()
    t = Transaction(
        type="consigne",
        campus=campus,
        total=credit,
        operator_label=operator_label or "",
        deposit_glasses=count,
        deposit_user_id=user.id,
        note=f"Retour de {count} verre(s) consigné(s)",
    )
    db.session.add(t)
    db.session.flush()
    w.glasses_outstanding = Wallet.glasses_outstanding - count
    w.balance = Wallet.balance + credit
    db.session.flush()
    if w.glasses_outstanding < 0:
        db.session.rollback()
        raise OperationError(
            "invalid",
            f"{user.display_name} n'a pas assez de verre(s) consigné(s).",
        )
    db.session.add(
        Contribution(
            transaction_id=t.id,
            user_id=user.id,
            campus=campus,
            amount=credit,
            balance_after=w.balance,
        )
    )
    db.session.commit()
    return t


def create_reload(*, operator_label, campus, user, amount_cents, payment_method):
    amount = _int_or_invalid(amount_cents, "Montant invalide.")
    if amount <= 0:
        raise OperationError("invalid", "Montant invalide.")
    if payment_method not in PAYMENT_METHODS:
        raise OperationError("invalid", "Moyen de paiement invalide.")
    w = _lock_wallets(campus, [user])[user.id]
    t = Transaction(
        type="rechargement",
        campus=campus,
        total=amount,
        operator_label=operator_label or "",
        payment_method=payment_method,
    )
    db.session.add(t)
    db.session.flush()
    w.balance = Wallet.balance + amount
    db.session.flush()
    db.session.add(
        Contribution(
            transaction_id=t.id,
            user_id=user.id,
            campus=campus,
            amount=amount,
            balance_after=w.balance,
        )
    )
    db.session.commit()
    return t


def create_withdrawal(*, operator_label, campus, user, amount_cents):
    amount = _int_or_invalid(amount_cents, "Montant invalide.")
    if amount <= 0:
        raise OperationError("invalid", "Montant invalide.")
    w = _lock_wallets(campus, [user])[user.id]
    if amount > w.balance:
        raise OperationError("solde", f"Solde insuffisant ({w.balance} < {amount}).")
    t = Transaction(
        type="retrait",
        campus=campus,
        total=amount,
        operator_label=operator_label or "",
    )
    db.session.add(t)
    db.session.flush()
    w.balance = Wallet.balance - amount
    db.session.flush()
    if w.balance < 0:
        db.session.rollback()
        raise OperationError("solde", "Solde insuffisant.")
    db.session.add(
        Contribution(
            transaction_id=t.id,
            user_id=user.id,
            campus=campus,
            amount=-amount,
            balance_after=w.balance,
        )
    )
    db.session.commit()
    return t


def create_transfer(*, operator_label, campus, from_users, to_users, amount_cents):
    """Transfert de N donneurs vers M receveurs pour un montant total fixé.

    Le montant est réparti en centimes entiers, sans jamais diviser un centime :
    chaque donneur paie ``part`` et chaque receveur reçoit ``part``, les
    centimes restants étant attribués aux premiers de chaque liste. La somme
    débitée et la somme créditée valent donc exactement ``amount``.
    """
    amount = _int_or_invalid(amount_cents, "Montant invalide.")
    if amount <= 0:
        raise OperationError("invalid", "Montant invalide.")
    donors = _unique_users(from_users, "Sélectionnez au moins un donneur.")
    recipients = _unique_users(to_users, "Sélectionnez au moins un receveur.")
    donor_ids = {u.id for u in donors}
    if donor_ids & {u.id for u in recipients}:
        raise OperationError("invalid", "Un compte ne peut pas être à la fois donneur et receveur.")
    if amount < len(donors) or amount < len(recipients):
        # Sinon une part nulle apparaîtrait : au moins 1 centime par compte.
        raise OperationError(
            "invalid", "Montant trop faible pour être réparti (1 centime minimum par compte)."
        )

    donor_shares = _split_shares(amount, len(donors))
    recipient_shares = _split_shares(amount, len(recipients))

    wallets = _lock_wallets(campus, donors + recipients)
    for u, share in zip(donors, donor_shares, strict=True):
        if share > wallets[u.id].balance:
            raise OperationError("solde", f"Solde de {u.display_name} insuffisant pour sa part.")

    t = Transaction(
        type="transfert",
        campus=campus,
        total=amount,
        operator_label=operator_label or "",
        # Conservés pour l'affichage/l'historique quand le transfert est simple.
        from_user_id=donors[0].id if len(donors) == 1 else None,
        to_user_id=recipients[0].id if len(recipients) == 1 else None,
    )
    db.session.add(t)
    db.session.flush()

    for u, share in zip(donors, donor_shares, strict=True):
        wallets[u.id].balance = Wallet.balance - share
    for u, share in zip(recipients, recipient_shares, strict=True):
        wallets[u.id].balance = Wallet.balance + share
    db.session.flush()

    # Contrôle post-écriture : aucun donneur ne doit passer négatif (un
    # transfert n'autorise pas le découvert, contrairement à un achat).
    for u in donors:
        if wallets[u.id].balance < 0:
            db.session.rollback()
            raise OperationError("solde", f"Solde de {u.display_name} insuffisant.")

    for u, share in zip(donors, donor_shares, strict=True):
        db.session.add(
            Contribution(
                transaction_id=t.id,
                user_id=u.id,
                campus=campus,
                amount=-share,
                balance_after=wallets[u.id].balance,
            )
        )
    for u, share in zip(recipients, recipient_shares, strict=True):
        db.session.add(
            Contribution(
                transaction_id=t.id,
                user_id=u.id,
                campus=campus,
                amount=share,
                balance_after=wallets[u.id].balance,
            )
        )
    db.session.commit()
    return t


def cancel_transaction(transaction, admin_password):
    if transaction.cancelled:
        raise OperationError("invalid", "Transaction déjà annulée.")
    if not S.check_admin_password(admin_password, transaction.campus):
        raise OperationError("admin_password_required", "Mot de passe administrateur requis.")

    # Portefeuilles concernés : regroupés par campus puis verrouillés par id
    # croissant (même ordre que les autres opérations, pas d'interblocage).
    pairs = set()
    for c in transaction.contributions:
        if c.user_id is not None:
            pairs.add((c.campus, c.user_id))
    for uid in (transaction.deposit_user_id, transaction.from_user_id, transaction.to_user_id):
        if uid is not None:
            pairs.add((transaction.campus, uid))
    by_campus = defaultdict(set)
    for campus, uid in pairs:
        by_campus[campus].add(uid)
    wallets = {}
    for campus, uids in by_campus.items():
        users = [u for u in (db.session.get(User, uid) for uid in sorted(uids)) if u is not None]
        wallets.update({(campus, uid): w for uid, w in _lock_wallets(campus, users).items()})

    def w_of(campus, user_id):
        w = wallets.get((campus, user_id))
        if w is None:
            u = db.session.get(User, user_id)
            w = _lock_wallets(campus, [u])[u.id]
            wallets[(campus, user_id)] = w
        return w

    # Fûts concernés : volume recalculé depuis les lignes (l'article de tireuse
    # conserve son keg_id après vidage), puis articles de tireuse réactivés.
    volumes = defaultdict(float)
    tap_numbers = defaultdict(set)
    for line in transaction.lines:
        article = db.session.get(Article, line.article_id) if line.article_id else None
        if article is None or not article.is_tap or not article.keg_id:
            continue
        volumes[article.keg_id] += (article.volume_cl or 0) * line.quantity / 100.0
        if article.tap_number is not None:
            tap_numbers[article.keg_id].add(article.tap_number)
    kegs = _lock_kegs(volumes.keys())

    if transaction.type in ("achat", "consigne"):
        for c in transaction.contributions:
            if c.user is not None:
                w = w_of(c.campus, c.user_id)
                w.balance = Wallet.balance + (-c.amount if c.amount < 0 else 0)
        if (
            transaction.type == "achat"
            and transaction.deposit_user_id
            and transaction.deposit_glasses
        ):
            u = db.session.get(User, transaction.deposit_user_id)
            if u:
                w = w_of(transaction.campus, u.id)
                w.glasses_outstanding = max(0, w.glasses_outstanding - transaction.deposit_glasses)
        if transaction.type == "consigne":
            for c in transaction.contributions:
                if c.user is not None:
                    w = w_of(c.campus, c.user_id)
                    w.balance = Wallet.balance - c.amount
                    w.glasses_outstanding = Wallet.glasses_outstanding + transaction.deposit_glasses
    elif transaction.type == "rechargement":
        for c in transaction.contributions:
            if c.user is not None:
                w = w_of(c.campus, c.user_id)
                w.balance = Wallet.balance - c.amount
    elif transaction.type == "retrait":
        for c in transaction.contributions:
            if c.user is not None:
                w = w_of(c.campus, c.user_id)
                w.balance = Wallet.balance + (-c.amount)
    elif transaction.type == "transfert":
        if transaction.contributions:
            # Annulation symétrique : on inverse chaque part enregistrée, ce
            # qui couvre aussi bien les transferts à plusieurs donneurs /
            # receveurs que les transferts migrés.
            for c in transaction.contributions:
                if c.user_id is not None:
                    w = w_of(c.campus, c.user_id)
                    w.balance = Wallet.balance - c.amount
        else:
            if transaction.from_user_id:
                w_of(transaction.campus, transaction.from_user_id).balance = (
                    Wallet.balance + transaction.total
                )
            if transaction.to_user_id:
                w_of(transaction.campus, transaction.to_user_id).balance = (
                    Wallet.balance - transaction.total
                )

    for keg_id, volume_l in volumes.items():
        keg = kegs.get(keg_id)
        if keg is not None:
            keg.remaining_l = Keg.remaining_l + volume_l
    db.session.flush()
    for keg_id, numbers in tap_numbers.items():
        keg = kegs.get(keg_id)
        if keg is not None:
            _reactivate_keg_articles(keg, numbers)

    transaction.cancelled = True
    transaction.cancelled_at = utcnow()
    db.session.commit()
    _invalidate_activity_cache()


def history_cutoff():
    days = S.int_setting("max_history_days")
    return utcnow() - timedelta(days=days)


def visible_transactions():
    return Transaction.query.filter(Transaction.created_at >= history_cutoff()).order_by(
        Transaction.created_at.desc()
    )


def describe_transaction(t):
    # Tolérant aux contributions orphelines (compte supprimé avant l'activation
    # des clés étrangères SQLite) : l'historique doit rester consultable.
    names = [
        c.user.display_name if c.user is not None else "Compte supprimé"
        for c in t.contributions
        if c.user_id is not None
    ]
    if t.type == "achat":
        label = "Achat — " + ", ".join(names)
        if t.deposit_glasses:
            label += f" (+{t.deposit_glasses} consigne(s))"
    elif t.type == "direct":
        label = "Paiement direct" + (
            f" ({PAYMENT_METHODS[t.payment_method]})" if t.payment_method in PAYMENT_METHODS else ""
        )
    elif t.type == "rechargement":
        label = (
            "Rechargement — "
            + ", ".join(names)
            + (
                f" ({PAYMENT_METHODS[t.payment_method]})"
                if t.payment_method in PAYMENT_METHODS
                else ""
            )
        )
    elif t.type == "retrait":
        label = "Retrait — " + ", ".join(names)
    elif t.type == "transfert":
        donors = [
            c.user.display_name for c in t.contributions if c.user_id is not None and c.amount < 0
        ]
        recipients = [
            c.user.display_name for c in t.contributions if c.user_id is not None and c.amount > 0
        ]
        if donors or recipients:
            label = "Transfert — " + ", ".join(donors) + " → " + ", ".join(recipients)
        else:
            label = "Transfert — " + " → ".join(names)
    elif t.type == "consigne":
        label = "Retour de consigne — " + ", ".join(names)
    else:
        label = t.type
    return label
