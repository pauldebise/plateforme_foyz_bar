from datetime import timedelta

from sqlalchemy import select

from app.extensions import db
from app.models import (
    Article,
    Contribution,
    Event,
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


def _split_shares(total, n):
    base = total // n
    shares = [base] * n
    shares[0] += total - base * n
    return shares


def search_students(query, campus=None, limit=15):
    q = (query or "").strip()
    stmt = select(User).order_by(User.last_name, User.first_name).limit(limit)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            db.or_(
                User.first_name.ilike(like),
                User.last_name.ilike(like),
                (User.first_name + " " + User.last_name).ilike(like),
                (User.last_name + " " + User.first_name).ilike(like),
                User.username.ilike(like),
            )
        )
    users = db.session.scalars(stmt).unique().all()
    results = []
    for u in users:
        w = u.wallet(campus) if campus else None
        results.append(
            {
                "id": u.id,
                "name": u.full_name,
                "promotion": u.promotion,
                "blacklist": u.blacklist,
                "blacklist_alcohol": u.blacklist_alcohol,
                "is_team": u.is_team,
                "balance": w.balance if w else 0,
                "glasses": w.glasses_outstanding if w else 0,
            }
        )
    return results


def _resolve_items(items, event_id=None):
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
):
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

    lines = _resolve_items(items, event_id=event_id)
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
                    f"{u.full_name} est blacklisté : la transaction est impossible.",
                )
    has_alcohol = any(a.is_alcohol for a, _ in lines) or any(
        a.article_type in ALCOHOL_TYPES for a, _ in lines
    )
    if not direct and has_alcohol:
        for u in users:
            if u.blacklist_alcohol:
                raise OperationError(
                    "alcohol",
                    f"{u.full_name} est blacklist alcool : commande avec alcool refusée.",
                )

    all_team = (not direct) and users and all(u.is_team for u in users)
    cart = []
    article_total = 0
    for a, qty in lines:
        unit = a.price_for(campus, team=all_team)
        line_total = unit * qty
        article_total += line_total
        cart.append({"article": a, "quantity": qty, "unit_price": unit, "line_total": line_total})

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

    negative = []
    if not direct:
        for u, share in zip(users, shares):
            w = _get_wallet(u, campus)
            new_balance = w.balance - share
            if new_balance < 0:
                negative.append((u, w, new_balance))
        for u, w, new_balance in negative:
            if -new_balance > S.overdraft_limit():
                raise OperationError(
                    "overdraft_limit",
                    f"Découvert maximum dépassé pour {u.full_name} : transaction refusée.",
                )
        if negative:
            if not S.check_admin_password(admin_password):
                raise OperationError(
                    "admin_password_required",
                    "Un étudiant passera en négatif : mot de passe administrateur requis.",
                    {"negative_users": [u.full_name for u, _, _ in negative]},
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
    )
    db.session.add(t)
    db.session.flush()
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
        if c["article"].is_tap and c["article"].keg_id:
            keg = c["article"].keg
            volume_l = (c["article"].volume_cl or 0) * c["quantity"] / 100.0
            keg.remaining_l = max(0.0, keg.remaining_l - volume_l)
            if keg.remaining_l <= 0.01:
                keg.remaining_l = 0.0
                _drain_tap(keg)

    if not direct:
        for u, share in zip(users, shares):
            w = _get_wallet(u, campus)
            w.balance -= share
            db.session.add(
                Contribution(
                    transaction_id=t.id,
                    user_id=u.id,
                    campus=campus,
                    amount=-share,
                    balance_after=w.balance,
                )
            )
        if glasses and primary is not None:
            wp = _get_wallet(primary, campus)
            wp.glasses_outstanding += glasses
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
    db.session.commit()
    return t


def _drain_tap(keg):
    for tap in db.session.scalars(select(Tap).where(Tap.keg_id == keg.id)):
        tap.keg_id = None
    for article in db.session.scalars(select(Article).where(Article.keg_id == keg.id)):
        article.active = False


def return_glasses(*, operator_label, campus, user, count):
    count = int(count or 0)
    if count <= 0:
        raise OperationError("invalid", "Nombre de verres invalide.")
    w = _get_wallet(user, campus)
    if w.glasses_outstanding < count:
        raise OperationError("invalid", f"{user.full_name} n'a que {w.glasses_outstanding} verre(s) consigné(s).")
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
    w.glasses_outstanding -= count
    w.balance += credit
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
    w = _get_wallet(user, campus)
    t = Transaction(
        type="rechargement",
        campus=campus,
        total=amount,
        operator_label=operator_label or "",
        payment_method=payment_method,
    )
    db.session.add(t)
    db.session.flush()
    w.balance += amount
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
    w = _get_wallet(user, campus)
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
    w.balance -= amount
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
    wf = _get_wallet(from_user, campus)
    wt = _get_wallet(to_user, campus)
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
    wf.balance -= amount
    wt.balance += amount
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

    deposit_value = S.deposit_value()
    if transaction.type in ("achat", "consigne"):
        for c in transaction.contributions:
            if c.user_id:
                w = _get_wallet(c.user, c.campus)
                w.balance += -c.amount if c.amount < 0 else 0
        if transaction.type == "achat" and transaction.deposit_user_id and transaction.deposit_glasses:
            u = db.session.get(User, transaction.deposit_user_id)
            if u:
                w = _get_wallet(u, transaction.campus)
                w.glasses_outstanding = max(0, w.glasses_outstanding - transaction.deposit_glasses)
        if transaction.type == "consigne":
            for c in transaction.contributions:
                if c.user_id:
                    w = _get_wallet(c.user, c.campus)
                    w.balance -= c.amount
                    w.glasses_outstanding += transaction.deposit_glasses
    elif transaction.type == "rechargement":
        for c in transaction.contributions:
            if c.user_id:
                w = _get_wallet(c.user, c.campus)
                w.balance -= c.amount
    elif transaction.type == "retrait":
        for c in transaction.contributions:
            if c.user_id:
                w = _get_wallet(c.user, c.campus)
                w.balance += -c.amount
    elif transaction.type == "transfert":
        if transaction.from_user_id:
            u = db.session.get(User, transaction.from_user_id)
            if u:
                _get_wallet(u, transaction.campus).balance += transaction.total
        if transaction.to_user_id:
            u = db.session.get(User, transaction.to_user_id)
            if u:
                _get_wallet(u, transaction.campus).balance -= transaction.total

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
    names = [c.user.full_name for c in t.contributions if c.user_id]
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
