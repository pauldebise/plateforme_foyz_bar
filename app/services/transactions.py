from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Article,
    Contribution,
    Event,
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
    TAP_SIZES,
    utcnow,
)


class OperationError(Exception):
    def __init__(self, code, message, extra=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra or {}


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
    base = total // n
    shares = [base] * n
    shares[0] += total - base * n
    return shares


def _recent_activity(campus=None, days=45):
    """Achats récents par utilisateur : (nombre d'achats, dernier achat)."""
    since = utcnow() - timedelta(days=days)
    stmt = (
        select(
            Contribution.user_id.label("user_id"),
            func.count(Transaction.id).label("recent_count"),
            func.max(Transaction.created_at).label("last_at"),
        )
        .join(Transaction, Transaction.id == Contribution.transaction_id)
        .where(
            Contribution.user_id.isnot(None),
            Transaction.cancelled.is_(False),
            Transaction.type == "achat",
            Transaction.created_at >= since,
        )
        .group_by(Contribution.user_id)
    )
    if campus:
        stmt = stmt.where(Contribution.campus == campus)
    return stmt.subquery()


def search_students(query, campus=None, limit=15):
    q = (query or "").strip()
    activity = _recent_activity(campus)
    # En tête : comptes les plus actifs récemment ; ensuite alphabétique.
    # La recherche porte sur le nom réel, le surnom et l'identifiant.
    stmt = select(User).outerjoin(activity, activity.c.user_id == User.id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(
            User.name.ilike(like),
            User.nickname.ilike(like),
            User.username.ilike(like),
        ))
    stmt = stmt.order_by(
        func.coalesce(activity.c.recent_count, 0).desc(),
        func.coalesce(activity.c.last_at, datetime(1970, 1, 1)).desc(),
        User.name.asc(),
    ).limit(limit)
    users = db.session.scalars(stmt).unique().all()
    results = []
    for u in users:
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
        try:
            aid = int(it.get("article_id"))
            qty = int(it.get("quantity", 0))
        except (TypeError, ValueError):
            raise OperationError("invalid", "Commande invalide.")
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
        if a is None:
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
    allow_negative=None,
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
        contributor_ids = [int(x) for x in (contributor_ids or [])]
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
        glasses = max(0, int(deposit_glasses))
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
        for u, share in zip(users, shares):
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
        if negative:
            if not S.check_admin_password(admin_password):
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
        for u, share in zip(users, shares):
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
        for u, share in zip(users, shares):
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
    count = int(count or 0)
    if count <= 0:
        raise OperationError("invalid", "Nombre de verres invalide.")
    w = _lock_wallets(campus, [user])[user.id]
    if w.glasses_outstanding < count:
        raise OperationError("invalid", f"{user.display_name} n'a que {w.glasses_outstanding} verre(s) consigné(s).")
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
    amount = int(amount_cents)
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
            transaction_id=t.id, user_id=user.id, campus=campus, amount=amount, balance_after=w.balance
        )
    )
    db.session.commit()
    return t


def create_withdrawal(*, operator_label, campus, user, amount_cents):
    amount = int(amount_cents)
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
            transaction_id=t.id, user_id=user.id, campus=campus, amount=-amount, balance_after=w.balance
        )
    )
    db.session.commit()
    return t


def create_transfer(*, operator_label, campus, from_user, to_user, amount_cents):
    amount = int(amount_cents)
    if amount <= 0:
        raise OperationError("invalid", "Montant invalide.")
    if from_user.id == to_user.id:
        raise OperationError("invalid", "Les deux comptes doivent être différents.")
    wallets = _lock_wallets(campus, [from_user, to_user])
    wf, wt = wallets[from_user.id], wallets[to_user.id]
    if amount > wf.balance:
        raise OperationError("solde", "Solde du donneur insuffisant.")
    t = Transaction(
        type="transfert",
        campus=campus,
        total=amount,
        operator_label=operator_label or "",
        from_user_id=from_user.id,
        to_user_id=to_user.id,
    )
    db.session.add(t)
    db.session.flush()
    wf.balance = Wallet.balance - amount
    wt.balance = Wallet.balance + amount
    db.session.flush()
    if wf.balance < 0:
        db.session.rollback()
        raise OperationError("solde", "Solde du donneur insuffisant.")
    db.session.add(
        Contribution(
            transaction_id=t.id, user_id=from_user.id, campus=campus, amount=-amount, balance_after=wf.balance
        )
    )
    db.session.add(
        Contribution(
            transaction_id=t.id, user_id=to_user.id, campus=campus, amount=amount, balance_after=wt.balance
        )
    )
    db.session.commit()
    return t


def cancel_transaction(transaction, admin_password):
    if transaction.cancelled:
        raise OperationError("invalid", "Transaction déjà annulée.")
    if not S.check_admin_password(admin_password):
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
        if transaction.type == "achat" and transaction.deposit_user_id and transaction.deposit_glasses:
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
        if transaction.from_user_id:
            w_of(transaction.campus, transaction.from_user_id).balance = Wallet.balance + transaction.total
        if transaction.to_user_id:
            w_of(transaction.campus, transaction.to_user_id).balance = Wallet.balance - transaction.total

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


def history_cutoff():
    days = S.int_setting("max_history_days")
    return utcnow() - timedelta(days=days)


def visible_transactions():
    return Transaction.query.filter(
        Transaction.created_at >= history_cutoff()
    ).order_by(Transaction.created_at.desc())


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
        label = "Paiement direct" + (f" ({PAYMENT_METHODS[t.payment_method]})" if t.payment_method in PAYMENT_METHODS else "")
    elif t.type == "rechargement":
        label = "Rechargement — " + ", ".join(names) + (f" ({PAYMENT_METHODS[t.payment_method]})" if t.payment_method in PAYMENT_METHODS else "")
    elif t.type == "retrait":
        label = "Retrait — " + ", ".join(names)
    elif t.type == "transfert":
        label = "Transfert — " + " → ".join(names)
    elif t.type == "consigne":
        label = "Retour de consigne — " + ", ".join(names)
    else:
        label = t.type
    return label
