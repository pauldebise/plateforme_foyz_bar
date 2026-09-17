"""Tests de concurrence des mouvements de solde (phase 3, T-3.1).

Exécutable sans pytest : python -m tests.test_concurrency

Deux encaissements simultanés sur le même compte doivent produire un solde
exact (pas de lost update) et le découvert maximum doit rester respecté.
SQLite n'applique pas SELECT … FOR UPDATE : c'est la mise à jour *relative*
des soldes (UPDATE … SET balance = balance ± :x) qui garantit l'absence de
perte, le contrôle de découvert étant refait après écriture.
"""

import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_conc_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'app.db'}"
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Article, User  # noqa: E402
from app.services import transactions as T  # noqa: E402
from app.services.settings import set_setting  # noqa: E402


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _seed(app, name, price, balance):
    with app.app_context():
        article = Article(
            name=name,
            article_type="biere",
            is_alcohol=True,
            price_std_brest=price,
            active=True,
        )
        user = User(
            name=name,
            username=name.lower().replace(" ", "."),
            password_hash=generate_password_hash("secret123"),
        )
        db.session.add_all([article, user])
        db.session.flush()
        user.wallet("brest").balance = balance
        db.session.commit()
        return article.id, user.id


def _run_threads(app, count, fn):
    barrier = threading.Barrier(count)
    results = []
    lock = threading.Lock()

    def worker():
        with app.app_context():
            barrier.wait()
            try:
                outcome = fn()
            except T.OperationError as exc:
                outcome = f"refus:{exc.code}"
            except Exception as exc:  # noqa: BLE001
                outcome = f"erreur:{exc!r}"
            with lock:
                results.append(outcome)

    threads = [threading.Thread(target=worker) for _ in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_purchases_do_not_lose_updates():
    app = create_app()
    article_id, user_id = _seed(app, "Concurrent", 100, 1000)

    def buy():
        t = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article_id, "quantity": 1}],
            contributor_ids=[user_id],
        )
        return f"ok:{t.id}"

    results = _run_threads(app, 10, buy)
    errors = [r for r in results if not r.startswith("ok:")]
    _expect(not errors, f"les 10 achats aboutissent ({errors[:1]})")
    with app.app_context():
        balance = db.session.get(User, user_id).wallet("brest").balance
    _expect(balance == 0, f"solde exact après 10 achats de 100 (obtenu {balance})")


def test_concurrent_overdraft_is_respected():
    app = create_app()
    article_id, user_id = _seed(app, "Decouvert", 600, 600)
    with app.app_context():
        set_setting("overdraft_limit_cents", "500")
        db.session.commit()

    def buy():
        t = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article_id, "quantity": 1}],
            contributor_ids=[user_id],
        )
        return f"ok:{t.id}"

    results = _run_threads(app, 2, buy)
    successes = [r for r in results if r.startswith("ok:")]
    refusals = [r for r in results if r.startswith("refus:")]
    _expect(len(successes) == 1 and len(refusals) == 1,
            f"un seul achat concurrent accepté ({results})")
    with app.app_context():
        balance = db.session.get(User, user_id).wallet("brest").balance
    _expect(balance == 0, f"solde = 600 - 600, jamais sous -500 (obtenu {balance})")


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
