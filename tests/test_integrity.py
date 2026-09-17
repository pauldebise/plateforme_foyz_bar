"""Tests d'intégrité comptable (phase 3, T-3.2 et T-3.3).

Exécutable sans pytest : python -m tests.test_integrity

- T-3.2 : idempotence des encaissements (même jeton = une seule transaction) ;
- T-3.3 : annulation d'achat -> restitution du fût et du catalogue pression.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_integrity_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'app.db'}"
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from sqlalchemy import func, inspect as sqla_inspect, select, text  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Article, Keg, Tap, Transaction, User  # noqa: E402
from app.services.transactions import cancel_transaction, create_purchase  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(html, field="csrf-token"):
    pattern = (
        r'name="csrf-token" content="([^"]+)"'
        if field == "csrf-token"
        else r'name="_csrf" value="([^"]+)"'
    )
    match = re.search(pattern, html)
    _expect(match is not None, f"jeton CSRF {field} présent")
    return match.group(1)


def _seed(app):
    with app.app_context():
        article = Article(
            name="Pinte standard",
            article_type="biere",
            is_alcohol=True,
            price_std_brest=300,
            active=True,
        )
        user = User(
            name="Élève Idem",
            username="eleve.idem",
            password_hash=generate_password_hash("secret123"),
        )
        db.session.add_all([article, user])
        db.session.flush()
        user.wallet("brest").balance = 1000
        db.session.commit()
        return {"article": article.id, "user": user.id}


def _login(client):
    html = client.get("/connexion").get_data(as_text=True)
    token = _csrf(html, "_csrf")
    res = client.post(
        "/connexion",
        data={
            "username": "admin",
            "password": ADMIN_PASSWORD,
            "campus": "brest",
            "_csrf": token,
        },
    )
    _expect(res.status_code == 302, f"connexion équipe réussie ({res.status_code})")


def test_purchase_idempotency_via_api():
    app = create_app()
    ids = _seed(app)
    client = app.test_client()
    _login(client)
    csrf = _csrf(client.get("/equipe/paiement").get_data(as_text=True))
    payload = {
        "items": [{"article_id": ids["article"], "quantity": 1}],
        "contributors": [ids["user"]],
        "idempotency_key": "clef-idempotence-0001",
    }
    first = client.post("/api/purchase", json=payload, headers={"X-CSRFToken": csrf})
    second = client.post("/api/purchase", json=payload, headers={"X-CSRFToken": csrf})
    _expect(first.status_code == 200, f"premier encaissement accepté ({first.status_code})")
    _expect(second.status_code == 200, f"rejeu accepté ({second.status_code})")
    _expect(
        first.get_json()["transaction_id"] == second.get_json()["transaction_id"],
        "le rejeu renvoie la même transaction",
    )
    with app.app_context():
        count = db.session.scalar(
            select(func.count(Transaction.id)).where(
                Transaction.idempotency_key == payload["idempotency_key"]
            )
        )
        _expect(count == 1, f"une seule transaction pour le jeton (obtenu {count})")
        balance = db.session.get(User, ids["user"]).wallet("brest").balance
        _expect(balance == 700, f"un seul débit de 300 (obtenu {balance})")


def test_cancel_restores_keg_and_tap_catalog():
    app = create_app()
    with app.app_context():
        keg = Keg(name="Fût test", volume_l=30.0, remaining_l=0.5)
        db.session.add(keg)
        db.session.flush()
        tap = Tap(number=501, name="Tireuse test", campus="brest", keg_id=keg.id)
        article = Article(
            name="Pinte de tireuse test",
            article_type="biere",
            volume_cl=50,
            is_alcohol=True,
            is_tap=True,
            tap_number=501,
            keg_id=keg.id,
            price_std_brest=200,
            active=True,
        )
        user = User(
            name="Acheteur Fût",
            username="acheteur.fut",
            password_hash=generate_password_hash("secret123"),
        )
        db.session.add_all([tap, article, user])
        db.session.flush()
        user.wallet("brest").balance = 1000
        db.session.commit()
        keg_id, tap_id, article_id, user_id = keg.id, tap.id, article.id, user.id

        transaction = create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article_id, "quantity": 1}],
            contributor_ids=[user_id],
        )
        transaction_id = transaction.id
        db.session.expire_all()

        _expect(db.session.get(Keg, keg_id).remaining_l == 0.0, "vente vide le fût")
        _expect(db.session.get(Article, article_id).active is False, "article tireuse désactivé")
        _expect(db.session.get(Tap, tap_id).keg_id is None, "tireuse détachée")

        cancel_transaction(db.session.get(Transaction, transaction_id), ADMIN_PASSWORD)
        db.session.expire_all()

        _expect(
            abs(db.session.get(Keg, keg_id).remaining_l - 0.5) < 1e-9,
            "volume du fût restauré",
        )
        _expect(db.session.get(Article, article_id).active is True, "article tireuse réactivé")
        _expect(db.session.get(Tap, tap_id).keg_id == keg_id, "tireuse rebranchée")
        _expect(
            db.session.get(User, user_id).wallet("brest").balance == 1000,
            "solde remboursé par l'annulation",
        )


def test_schema_upgrade_adds_idempotency_key():
    from app import ensure_schema_upgrades

    app = create_app()
    with app.app_context():
        with db.engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_transactions_idempotency_key"))
            conn.execute(text("ALTER TABLE transactions DROP COLUMN idempotency_key"))
        ensure_schema_upgrades()
        columns = {c["name"] for c in sqla_inspect(db.engine).get_columns("transactions")}
        _expect("idempotency_key" in columns, "colonne idempotency_key rétablie")
        indexes = {ix["name"] for ix in sqla_inspect(db.engine).get_indexes("transactions")}
        _expect(
            "ix_transactions_idempotency_key" in indexes,
            "index unique d'idempotence rétabli",
        )


def main():
    tests = [(name, fn) for name, fn in sorted(globals().items())
             if name.startswith("test_") and callable(fn)]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  OK   {name}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
