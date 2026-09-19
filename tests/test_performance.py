"""Tests de performance (phase 7).

Exécutable sans pytest : python -m tests.test_performance

Couvre :
- T-7.1 : les agrégats SQL (ventes, étudiants, articles, trésorerie) donnent
  exactement les mêmes valeurs qu'un calcul en Python de référence ;
- T-7.2 : index sur transaction_lines.article_id, pagination de l'historique
  et de la liste des comptes (plus de troncature silencieuse) ;
- T-7.3 : cache navigateur des assets et compression Brotli/gzip.
"""

import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_perf_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'app.db'}"
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)


from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import (  # noqa: E402
    Article,
    Contribution,
    Event,
    Transaction,
    TransactionLine,
    User,
)
from app.services.stats import sales_stats, students_stats, top_articles_stats  # noqa: E402
from app.services.transactions import history_cutoff  # noqa: E402
from app.services.treasury import treasury  # noqa: E402
from app.utils import paris_to_utc, to_paris, utcnow  # noqa: E402

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


def _login(client, username="admin", password=ADMIN_PASSWORD):
    html = client.get("/connexion").get_data(as_text=True)
    token = _csrf(html, "_csrf")
    res = client.post(
        "/connexion",
        data={"username": username, "password": password, "campus": "brest", "_csrf": token},
    )
    _expect(res.status_code == 302, f"connexion réussie ({res.status_code})")


def _add_transaction(
    campus,
    ttype,
    when,
    lines=(),
    contributors=(),
    total=None,
    payment_method=None,
    cancelled=False,
    event_id=None,
):
    t = Transaction(
        campus=campus,
        type=ttype,
        created_at=when,
        cancelled=cancelled,
        payment_method=payment_method,
        event_id=event_id,
    )
    for user, amount in contributors:
        t.contributions.append(
            Contribution(user_id=user.id, campus=campus, amount=amount, balance_after=0)
        )
    for article, name, atype, qty, unit in lines:
        t.lines.append(
            TransactionLine(
                article_id=article.id if article else None,
                article_name=name,
                article_type=atype,
                quantity=qty,
                unit_price=unit,
                line_total=qty * unit,
            )
        )
    t.total = total if total is not None else sum(line.line_total for line in t.lines)
    db.session.add(t)
    return t


def test_a_sql_aggregates_match_reference():
    app = create_app()
    now = to_paris(utcnow()).replace(tzinfo=None)
    day = now - timedelta(days=2)

    with app.app_context():
        art_b = Article(
            name="Pinte",
            article_type="biere",
            is_alcohol=True,
            campus="brest",
            price_std=300,
            active=True,
        )
        art_s = Article(
            name="Chips", article_type="snack", campus="brest", price_std=200, active=True
        )
        a = User(name="Alpha", username="perf.alpha", promotion=2025)
        b = User(
            name="Bravo",
            username="perf.bravo",
            promotion=2025,
            team_status="mandat",
            team_campus="brest",
        )
        c = User(name="Charlie", username="perf.charlie", promotion=2024)
        db.session.add_all([art_b, art_s, a, b, c])
        db.session.flush()

        event = Event(
            name="Soirée",
            campus="brest",
            starts_at=day,
            ends_at=day + timedelta(hours=4),
            token="perf-token",
            closed=False,
        )
        db.session.add(event)
        db.session.flush()

        # T1 : achat partagé A+B (bière 600 + snack 200)
        _add_transaction(
            "brest",
            "achat",
            day,
            [
                (art_b, "Pinte", "biere", 2, 300),
                (art_s, "Chips", "snack", 1, 200),
            ],
            [(a, 400), (b, 400)],
        )
        # T2 : achat C (bière 300)
        _add_transaction(
            "brest",
            "achat",
            day,
            [
                (art_b, "Pinte", "biere", 1, 300),
            ],
            [(c, 300)],
        )
        # T3 : paiement direct Paris sans participant
        _add_transaction(
            "paris",
            "direct",
            day,
            [
                (art_b, "Pinte", "biere", 1, 300),
            ],
        )
        # T4 : achat annulé (ignoré)
        _add_transaction(
            "brest",
            "achat",
            day,
            [
                (art_b, "Pinte", "biere", 5, 300),
            ],
            [(a, 1500)],
            cancelled=True,
        )
        # T5 : rechargement (trésorerie), à la frontière de mois hiver (T-7.1)
        winter = datetime(now.year, 1, 1, 0, 30)
        _add_transaction(
            "brest", "rechargement", paris_to_utc(winter), total=1000, payment_method="cb"
        )
        # T6 : achat rattaché à un événement
        _add_transaction(
            "brest",
            "achat",
            day,
            [
                (art_s, "Chips", "snack", 1, 200),
            ],
            [(a, 200)],
            event_id=event.id,
        )
        # T7 : ligne sans article (regroupée par nom)
        _add_transaction(
            "brest",
            "direct",
            day,
            [
                (None, "Crêpe maison", "snack", 3, 100),
            ],
        )
        db.session.commit()

        # --- Ventes : bière 1200 (4), snack 700 (5), total 1900 (9)
        stats = sales_stats({})
        _expect(
            stats["grand_total"] == {"qty": 9, "revenue": 1900},
            f"ventes totales ({stats['grand_total']})",
        )
        _expect(stats["by_category"]["biere"] == {"qty": 4, "revenue": 1200}, "ventes bière")
        _expect(stats["by_category"]["snack"] == {"qty": 5, "revenue": 700}, "ventes snack")
        _expect(sum(stats["series"].values()) == 1900, "série journalière = total")

        # Filtre catégorie
        only_beer = sales_stats({"category": "biere"})
        _expect(only_beer["grand_total"] == {"qty": 4, "revenue": 1200}, "filtre catégorie bière")

        # Filtre promotion/équipe (portée participant)
        team = sales_stats({"team_only": "team"})
        _expect(
            team["grand_total"] == {"qty": 3, "revenue": 800},
            f"ventes de l'équipe ({team['grand_total']})",
        )
        promo24 = sales_stats({"promotion": "2024"})
        _expect(promo24["grand_total"] == {"qty": 1, "revenue": 300}, "ventes promo 2024")

        # --- Étudiants : A 600/2, B 400/1, C 300/1 (T3 sans participant, T4 annulée)
        students = students_stats({}, per_page=25)
        rows = {s["name"]: s for s in students["rows"]}
        _expect(set(rows) == {"Alpha", "Bravo", "Charlie"}, f"participants ({set(rows)})")
        _expect(
            rows["Alpha"]["spent"] == 600
            and rows["Alpha"]["nb"] == 2
            and rows["Alpha"]["articles"] == 2.5,
            "Alpha réparti sur 2 achats",
        )
        _expect(
            rows["Bravo"]["spent"] == 400 and rows["Bravo"]["nb"] == 1,
            "Bravo part du premier achat",
        )
        _expect(rows["Charlie"]["spent"] == 300, "Charlie 300")
        _expect(students["total"] == 3, "3 étudiants")

        # Pagination et recherche
        paged = students_stats({}, per_page=2)
        _expect(
            paged["total"] == 3 and paged["pages"] == 2 and len(paged["rows"]) == 2,
            "pagination étudiants",
        )
        found = students_stats({}, search="brâv")
        _expect(
            len(found["rows"]) == 1 and found["rows"][0]["name"] == "Bravo",
            "recherche insensible aux accents",
        )

        # --- Articles : Pinte 4/1200, Crêpe 3/300, Chips 2/400
        articles = top_articles_stats({})
        names = [a["name"] for a in articles]
        _expect(names == ["Pinte", "Crêpe maison", "Chips"], f"classement articles ({names})")
        _expect(articles[0]["qty"] == 4 and articles[0]["revenue"] == 1200, "Pinte agrégée")

        # --- Trésorerie (Brest) : rechargement 1000 (janvier), ventes 1700,
        # événement 200 (hors ventes par type)
        report = treasury("brest")
        by_label = {e["label"]: e for e in report}
        winter_entry = by_label[f"{winter.month:02d}/{winter.year}"]
        _expect(winter_entry["reloads_total"] == 1000, "rechargement au bon mois (TZ Paris)")
        _expect(winter_entry["reloads_by_method"]["cb"] == 1000, "rechargement par CB")
        current_entry = by_label[f"{now.month:02d}/{now.year}"]
        _expect(current_entry["sales_total"] == 1400, "consommation virtuelle")
        _expect(current_entry["sales_by_type"]["biere"] == 900, "ventes bière du mois")
        _expect(current_entry["sales_by_type"]["snack"] == 500, "ventes snack du mois")
        _expect(current_entry["events_total"] == 200, "recettes événement")
        _expect(current_entry["grand_total"] == 200, "entrées du mois = événement")
        _expect(winter_entry["grand_total"] == 1000, "entrées de janvier = rechargement")


def test_b_index_and_pagination():
    app = create_app()
    with app.app_context():
        indexes = {
            r[0]
            for r in db.session.execute(
                db.text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name='transaction_lines'"
                )
            )
        }
        _expect("ix_transaction_lines_article_id" in indexes, "index article_id présent (T-7.2)")
        _expect(history_cutoff() is not None, "fenêtre d'historique disponible")

        admin = db.session.scalars(db.select(User).where(User.username == "admin")).first()
        _expect(admin is not None, "compte admin créé")
        base = utcnow() - timedelta(days=1)
        for i in range(60):
            _add_transaction(
                "brest", "rechargement", base + timedelta(minutes=i), total=100, payment_method="cb"
            )
            t = db.session.scalars(
                db.select(Transaction).order_by(Transaction.id.desc()).limit(1)
            ).first()
            t.operator_label = f"OP{i:02d}"
        for i in range(60):
            db.session.add(User(name=f"Compte {i:02d}", username=f"perf.compte{i:02d}"))
        db.session.commit()

    client = app.test_client()
    _login(client)

    first = client.get("/equipe/historique").get_data(as_text=True)
    _expect("page 1 / 2" in first, "historique paginé (page 1 / 2)")
    _expect("OP59" in first and "OP00" not in first, "page 1 = plus récentes")

    second = client.get("/equipe/historique?page=2").get_data(as_text=True)
    _expect("page 2 / 2" in second, "historique page 2")
    _expect("OP00" in second and "OP59" not in second, "page 2 = plus anciennes")

    comptes = client.get("/admin/comptes?page=2").get_data(as_text=True)
    _expect("Compte 59" in comptes, "comptes paginés : page 2 atteint le dernier")
    _expect("Compte 00" not in comptes, "comptes : la page 1 n'est pas répétée")


def test_c_cache_and_compression():
    app = create_app()
    client = app.test_client()

    css = client.get("/static/vendor/bootstrap/bootstrap.min.css")
    _expect(css.status_code == 200, "asset statique servi")
    _expect(
        "max-age=31536000" in (css.headers.get("Cache-Control") or "")
        and "immutable" in (css.headers.get("Cache-Control") or ""),
        "cache navigateur long sur /static (assets versionnés)",
    )

    upload = Path(os.environ["UPLOAD_DIR"]) / "logo.png"
    upload.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    res = client.get("/uploads/logo.png")
    _expect(
        "max-age=31536000" in (res.headers.get("Cache-Control") or "")
        and "immutable" in (res.headers.get("Cache-Control") or ""),
        "cache navigateur long sur /uploads (noms horodatés)",
    )

    compressed = client.get(
        "/static/vendor/bootstrap/bootstrap.min.css",
        headers={"Accept-Encoding": "br, gzip"},
    )
    _expect(
        compressed.headers.get("Content-Encoding") in ("br", "gzip"),
        f"asset compressé ({compressed.headers.get('Content-Encoding')})",
    )
    _expect("Accept-Encoding" in (compressed.headers.get("Vary") or ""), "Vary: Accept-Encoding")

    # Utilisable sans Internet : la page ne référence aucun hôte externe.
    page = client.get("/connexion").get_data(as_text=True)
    _expect(
        'src="http' not in page and 'href="http' not in page,
        "aucune ressource externe (hors ligne)",
    )


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
    sys.exit(main())
