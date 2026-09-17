"""Test de charge : simulation d'une soirée de bar (P10-4).

Exécutable sans pytest : python -m tests.test_charge

Plusieurs caissiers encaissent en parallèle (achats multi-articles, rechargements,
rejeux « double clic »), puis on vérifie :
- aucune erreur ni refus inattendu ;
- conservation comptable exacte (soldes finaux = soldes initiaux + rechargements
  - achats) et découvert jamais dépassé ;
- idempotence : un rejeu avec la même clé ne crée pas de seconde transaction ;
- latences (p50/p95/p99) et débit, imprimés pour comparaison entre versions.

Barème volontairement modeste : `FOYZ_CHARGE_SCALE` (défaut 1) multiplie le
nombre de caissiers et de ventes ; `FOYZ_CHARGE_SCALE=8` simule une grosse
soirée (~1 200 transactions). Le seuil d'échec du p95 reste large (5 s) pour
ne pas dépendre de la machine, l'objectif étant de détecter une régression
franche, pas de mesurer finement.
"""

import os
import random
import statistics
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_charge_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from sqlalchemy import func, select  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Article, Transaction, User  # noqa: E402
from app.services import transactions as T  # noqa: E402

SCALE = max(1.0, float(os.environ.get("FOYZ_CHARGE_SCALE", "1")))
CASHIERS = max(2, int(3 * SCALE))
SALES_PER_CASHIER = max(8, int(25 * SCALE))
STUDENTS = 60
ARTICLES = 6
INITIAL_BALANCE = 20_000  # 200,00 € par étudiant
OVERDRAFT = 500


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _seed(app):
    with app.app_context():
        articles = [
            Article(
                name=f"Charge {i}",
                article_type="biere",
                is_alcohol=True,
                price_std_brest=100 + 50 * i,
                price_team_brest=80 + 40 * i,
                active=True,
            )
            for i in range(ARTICLES)
        ]
        students = [
            User(
                name=f"Élève Charge {i}",
                username=f"charge.eleve{i}",
                password_hash=generate_password_hash("secret123"),
            )
            for i in range(STUDENTS)
        ]
        operators = [
            User(
                name=f"Barman {i}",
                username=f"charge.barman{i}",
                team_status="mandat",
                team_campus="brest",
                password_hash=generate_password_hash("secret123"),
            )
            for i in range(CASHIERS)
        ]
        db.session.add_all(articles + students + operators)
        db.session.flush()
        for student in students:
            student.wallet("brest").balance = INITIAL_BALANCE
        db.session.commit()
        return (
            [a.id for a in articles],
            [s.id for s in students],
            [f"barman-{i}" for i in range(CASHIERS)],
        )


def test_soiree_sous_charge():
    app = create_app()
    article_ids, student_ids, operator_labels = _seed(app)
    barrier = threading.Barrier(CASHIERS)
    latencies = []
    errors = []
    refusals = []
    lock = threading.Lock()

    def cashier(index):
        rng = random.Random(1000 + index)
        operator = operator_labels[index]
        with app.app_context():
            barrier.wait()
            for sale in range(SALES_PER_CASHIER):
                items = [
                    {
                        "article_id": rng.choice(article_ids),
                        "quantity": rng.choice([1, 1, 2, 3]),
                    }
                    for _ in range(rng.choice([1, 1, 1, 2]))
                ]
                contributor = rng.choice(student_ids)
                key = str(uuid.uuid4())
                started = time.perf_counter()
                try:
                    transaction = T.create_purchase(
                        operator_label=operator,
                        campus="brest",
                        items=items,
                        contributor_ids=[contributor],
                        idempotency_key=key,
                    )
                    if sale % 7 == 0:
                        replay = T.create_purchase(
                            operator_label=operator,
                            campus="brest",
                            items=items,
                            contributor_ids=[contributor],
                            idempotency_key=key,
                        )
                        if replay.id != transaction.id:
                            with lock:
                                errors.append("rejeu non dédupliqué")
                    if sale % 11 == 0:
                        student = db.session.get(User, rng.choice(student_ids))
                        T.create_reload(
                            operator_label=operator,
                            campus="brest",
                            user=student,
                            amount_cents=500,
                            payment_method="cb",
                        )
                    with lock:
                        latencies.append(time.perf_counter() - started)
                except T.OperationError as exc:
                    with lock:
                        refusals.append(exc.code)
                except Exception as exc:  # noqa: BLE001 - rapport de charge
                    with lock:
                        errors.append(repr(exc))

    started = time.perf_counter()
    threads = [threading.Thread(target=cashier, args=(i,)) for i in range(CASHIERS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.perf_counter() - started

    _expect(not errors, f"aucune erreur sous charge ({errors[:3]})")
    _expect(not refusals, f"aucun refus inattendu ({refusals[:3]})")
    expected_sales = CASHIERS * SALES_PER_CASHIER
    _expect(
        len(latencies) == expected_sales,
        f"toutes les ventes mesurées ({len(latencies)}/{expected_sales})",
    )

    with app.app_context():
        purchases = db.session.scalar(
            select(func.coalesce(func.sum(Transaction.total), 0)).where(
                Transaction.type.in_(("achat", "direct"))
            )
        )
        reloads = db.session.scalar(
            select(func.coalesce(func.sum(Transaction.total), 0)).where(
                Transaction.type == "rechargement"
            )
        )
        purchase_count = db.session.scalar(
            select(func.count()).select_from(Transaction).where(Transaction.type == "achat")
        )
        _expect(
            purchase_count == expected_sales,
            f"une transaction par vente, rejeux dédupliqués ({purchase_count})",
        )
        students = db.session.scalars(select(User).where(User.username.like("charge.eleve%"))).all()
        balances = [student.wallet("brest").balance for student in students]
        _expect(
            all(balance >= -OVERDRAFT for balance in balances),
            "découvert jamais dépassé",
        )
        final_total = sum(balances)
        expected_total = INITIAL_BALANCE * STUDENTS + reloads - purchases
        _expect(
            final_total == expected_total,
            f"conservation comptable (attendu {expected_total}, obtenu {final_total})",
        )

    ordered = sorted(latencies)
    p50 = statistics.median(ordered)
    p95 = ordered[max(0, int(len(ordered) * 0.95) - 1)]
    p99 = ordered[max(0, int(len(ordered) * 0.99) - 1)]
    throughput = len(ordered) / elapsed if elapsed else 0
    print(
        f"    soirée : {CASHIERS} caissier(s) × {SALES_PER_CASHIER} ventes, "
        f"{elapsed:.1f} s, {throughput:.1f} ventes/s"
    )
    print(
        f"    latences : p50 {p50 * 1000:.0f} ms, p95 {p95 * 1000:.0f} ms, p99 {p99 * 1000:.0f} ms"
    )
    _expect(p95 < 5.0, f"p95 sous 5 s ({p95 * 1000:.0f} ms)")


def main():
    tests = [
        (name, fn)
        for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    failures = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  OK   {name}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL {name}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} tests OK")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
