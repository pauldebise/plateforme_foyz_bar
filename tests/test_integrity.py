"""Tests d'intégrité comptable (phase 3, T-3.2 et T-3.3).

Exécutable sans pytest : python -m tests.test_integrity

- T-3.2 : idempotence des encaissements (même jeton = une seule transaction) ;
- T-3.3 : la vente ne vide plus le fût ni le catalogue pression ; l'annulation
  rembourse sans toucher aux tireuses.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_integrity_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
IS_SQLITE = os.environ["DATABASE_URL"].startswith("sqlite")
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
from app.utils import TAP_SIZES  # noqa: E402

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
            campus="brest",
            price_std=300,
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


def test_sale_does_not_drain_keg_or_tap_catalog():
    app = create_app()
    with app.app_context():
        keg = Keg(name="Fût test", volume_l=30.0)
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
            campus="brest",
            price_std=200,
            active=True,
        )
        user = User(
            name="Acheteur Fût",
            username="acheteur.fut",
            password_hash=generate_password_hash("secret123"),
        )
        db.session.add_all([tap, article, user])
        db.session.flush()
        user.wallet("brest").balance = 100_000
        db.session.commit()
        keg_id, tap_id, article_id, user_id = keg.id, tap.id, article.id, user.id

        # 100 pintes = 50 L, au-delà des 30 L déclarés : l'ancien compteur
        # aurait vidé le fût. La tireuse doit rester en service.
        transaction = create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article_id, "quantity": 100}],
            contributor_ids=[user_id],
        )
        transaction_id = transaction.id
        db.session.expire_all()

        _expect(db.session.get(Tap, tap_id).keg_id == keg_id, "tireuse toujours montée")
        _expect(
            db.session.get(Article, article_id).active is True,
            "article de tireuse toujours actif",
        )
        _expect(
            db.session.get(User, user_id).wallet("brest").balance == 80_000,
            "débit de la vente",
        )

        cancel_transaction(db.session.get(Transaction, transaction_id), ADMIN_PASSWORD)
        db.session.expire_all()

        _expect(
            db.session.get(Tap, tap_id).keg_id == keg_id, "tireuse toujours montée après annulation"
        )
        _expect(
            db.session.get(Article, article_id).active is True,
            "article de tireuse toujours actif après annulation",
        )
        _expect(
            db.session.get(User, user_id).wallet("brest").balance == 100_000,
            "solde remboursé par l'annulation",
        )


def test_tireuse_accepts_keg_without_stock():
    app = create_app()
    with app.app_context():
        keg = Keg(name="Fût sans stock", volume_l=30.0)
        tap = Tap(number=502, name="Tireuse assignation", campus="brest")
        db.session.add_all([keg, tap])
        db.session.commit()
        keg_id = keg.id

    client = app.test_client()
    _login(client)
    res = client.get("/admin/tireuses")
    _expect(res.status_code == 200, "onglet tireuses accessible")
    html = res.get_data(as_text=True)
    _expect(
        f'<option value="{keg_id}">Fût sans stock</option>' in html,
        "fût proposé à l'assignation sans condition de volume",
    )
    _expect('name="remaining_l"' not in html, "champ restant absent de l'onglet tireuses")


def test_fut_sans_champ_actif_et_articles_encaissables():
    app = create_app()
    with app.app_context():
        keg = Keg(name="Fût assignable", volume_l=30.0, alcohol_degree=6.5)
        tap = Tap(number=503, name="Tireuse assignable", campus="brest")
        db.session.add_all([keg, tap])
        db.session.flush()
        row = keg.price_row("brest")
        row.price_half_std = 200
        row.price_pint_std = 400
        row.price_pot_std = 300
        db.session.commit()
        keg_id, tap_id, tap_number = keg.id, tap.id, tap.number

    client = app.test_client()
    _login(client)
    page = client.get(f"/admin/tireuses/kegs/{keg_id}").get_data(as_text=True)
    _expect("Fût actif" not in page, "champ « fût actif » retiré du formulaire")
    _expect('name="active"' not in page, "aucune case active sur le fût")

    token = _csrf(client.get("/admin/tireuses").get_data(as_text=True))
    res = client.post(
        f"/admin/tireuses/taps/{tap_id}/assigner",
        data={"keg_id": str(keg_id), "_csrf": token},
    )
    _expect(res.status_code == 302, f"assignation acceptée ({res.status_code})")
    with app.app_context():
        articles = db.session.scalars(
            select(Article).where(Article.tap_number == tap_number, Article.is_tap.is_(True))
        ).all()
        _expect(len(articles) == len(TAP_SIZES), "trois articles pression créés")
        _expect(all(a.active for a in articles), "articles pression encaissables")
        by_volume = {article.volume_cl: article for article in articles}
        _expect(
            by_volume[50].name == "Pinte de Tireuse assignable (Fût assignable - 6.5°)",
            f"nom avec degré du fût ({by_volume[50].name})",
        )


def test_meme_fut_sur_deux_tireuses():
    app = create_app()
    with app.app_context():
        keg = Keg(name="Cidre Kerne", volume_l=30.0)
        taps = [
            Tap(number=505, name="Tireuse cidre 1", campus="brest"),
            Tap(number=506, name="Tireuse cidre 2", campus="brest"),
        ]
        db.session.add(keg)
        db.session.add_all(taps)
        db.session.flush()
        row = keg.price_row("brest")
        row.price_half_std = 200
        row.price_pint_std = 400
        row.price_pot_std = 300
        db.session.commit()
        keg_id = keg.id
        tap_ids = [tap.id for tap in taps]
        tap_numbers = [tap.number for tap in taps]

    client = app.test_client()
    _login(client)
    for index, tap_id in enumerate(tap_ids):
        page = client.get("/admin/tireuses").get_data(as_text=True)
        _expect(
            f'<option value="{keg_id}">Cidre Kerne</option>' in page,
            "fût déjà monté encore proposé sur une autre tireuse",
        )
        token = _csrf(page)
        res = client.post(
            f"/admin/tireuses/taps/{tap_id}/assigner",
            data={"keg_id": str(keg_id), "_csrf": token},
        )
        _expect(res.status_code == 302, f"assignation {index + 1} acceptée")

    with app.app_context():
        for number in tap_numbers:
            tap = db.session.scalars(select(Tap).where(Tap.number == number)).one()
            _expect(tap.keg_id == keg_id, "même fût monté sur les deux tireuses")
            articles = db.session.scalars(
                select(Article).where(
                    Article.tap_number == number,
                    Article.is_tap.is_(True),
                    Article.active.is_(True),
                )
            ).all()
            _expect(len(articles) == len(TAP_SIZES), "articles propres à chaque tireuse")


def test_modification_fut_regenere_toujours_les_articles():
    app = create_app()
    with app.app_context():
        keg = Keg(name="Fût à modifier", volume_l=30.0)
        tap = Tap(number=507, name="Tireuse à modifier", campus="brest")
        db.session.add_all([keg, tap])
        db.session.flush()
        row = keg.price_row("brest")
        row.price_half_std = 200
        row.price_pint_std = 400
        row.price_pot_std = 300
        db.session.commit()
        keg_id, tap_id, tap_number = keg.id, tap.id, tap.number

    client = app.test_client()
    _login(client)
    token = _csrf(client.get("/admin/tireuses").get_data(as_text=True))
    res = client.post(
        f"/admin/tireuses/taps/{tap_id}/assigner",
        data={"keg_id": str(keg_id), "_csrf": token},
    )
    _expect(res.status_code == 302, "fût monté")

    token = _csrf(client.get(f"/admin/tireuses/kegs/{keg_id}").get_data(as_text=True))
    res = client.post(
        f"/admin/tireuses/kegs/{keg_id}",
        data={
            "name": "Fût à modifier",
            "alcohol_degree": "5",
            "volume_l": "30",
            "brest_half_std": "2.50",
            "brest_pint_std": "5.00",
            "brest_pot_std": "4.00",
            "brest_half_team": "2.00",
            "brest_pint_team": "4.00",
            "brest_pot_team": "3.00",
            "_csrf": token,
        },
    )
    _expect(res.status_code == 302, "modification du fût acceptée")

    with app.app_context():
        articles = db.session.scalars(
            select(Article).where(
                Article.tap_number == tap_number,
                Article.is_tap.is_(True),
                Article.active.is_(True),
            )
        ).all()
        by_volume = {article.volume_cl: article for article in articles}
        _expect(len(by_volume) == len(TAP_SIZES), "articles régénérés")
        _expect(by_volume[25].price_std == 250, "prix demi mis à jour sans case à cocher")
        _expect(by_volume[50].price_std == 500, "prix pinte mis à jour sans case à cocher")
        _expect(by_volume[33].price_std == 400, "prix pot mis à jour sans case à cocher")
        _expect(
            by_volume[50].name == "Pinte de Tireuse à modifier (Fût à modifier - 5.0°)",
            f"nom régénéré avec le nouveau degré ({by_volume[50].name})",
        )


def test_creation_fut_exige_tous_les_prix_et_accepte_zero():
    app = create_app()
    client = app.test_client()
    _login(client)

    form = {"name": "Fût prix explicites", "alcohol_degree": "5", "volume_l": "30"}
    for campus in ("brest", "paris"):
        for size in ("half", "pint", "pot"):
            for kind in ("std", "team"):
                form[f"{campus}_{size}_{kind}"] = "0"

    incomplete = {key: value for key, value in form.items() if key != "paris_pint_team"}
    token = _csrf(client.get("/admin/tireuses").get_data(as_text=True))
    res = client.post(
        "/admin/tireuses/kegs/nouveau",
        data={**incomplete, "_csrf": token},
    )
    _expect(res.status_code == 302, "prix manquant refusé proprement")
    with app.app_context():
        _expect(
            db.session.scalar(select(Keg).where(Keg.name == "Fût prix explicites")) is None,
            "aucun fût créé sans tous les prix",
        )

    token = _csrf(client.get("/admin/tireuses").get_data(as_text=True))
    res = client.post(
        "/admin/tireuses/kegs/nouveau",
        data={**form, "_csrf": token},
    )
    _expect(res.status_code == 302, "prix explicitement à 0 acceptés")
    with app.app_context():
        keg = db.session.scalars(select(Keg).where(Keg.name == "Fût prix explicites")).one()
        _expect(
            {price.campus for price in keg.prices} == {"brest", "paris"},
            "prix enregistrés pour les deux campus",
        )
        _expect(
            all(
                price.price_half_std == 0
                and price.price_pint_std == 0
                and price.price_pot_std == 0
                and price.price_half_team == 0
                and price.price_pint_team == 0
                and price.price_pot_team == 0
                for price in keg.prices
            ),
            "prix à 0 conservés",
        )


def test_recherche_fut_dans_la_carte_des_futs():
    app = create_app()
    with app.app_context():
        keg = Keg(name="Fût recherché unique", volume_l=30.0)
        db.session.add(keg)
        db.session.commit()

    client = app.test_client()
    _login(client)
    html = client.get("/admin/tireuses").get_data(as_text=True)
    _expect('id="keg-search"' in html, "barre de recherche présente")
    _expect(
        'data-keg-row data-search="Fût recherché unique"' in html,
        "ligne de fût indexée pour la recherche",
    )
    _expect('id="keg-search-empty"' in html, "message de recherche sans résultat présent")
    _expect("js/admin_tireuses.js" in html, "script de recherche chargé")


def test_schema_upgrade_adds_idempotency_key():
    if not IS_SQLITE:
        print("  (ignoré : évolution de schéma SQLite)")
        return
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


def test_schema_upgrade_drops_keg_remaining():
    if not IS_SQLITE:
        print("  (ignoré : évolution de schéma SQLite)")
        return
    from app import ensure_schema_upgrades

    app = create_app()
    with app.app_context():
        columns = {c["name"] for c in sqla_inspect(db.engine).get_columns("kegs")}
        if "remaining_l" not in columns:
            with db.engine.begin() as conn:
                conn.execute(
                    text("ALTER TABLE kegs ADD COLUMN remaining_l FLOAT NOT NULL DEFAULT 0")
                )
        ensure_schema_upgrades()
        columns = {c["name"] for c in sqla_inspect(db.engine).get_columns("kegs")}
        _expect("remaining_l" not in columns, "colonne remaining_l retirée")


def test_schema_upgrade_drops_keg_active():
    if not IS_SQLITE:
        print("  (ignoré : évolution de schéma SQLite)")
        return
    from app import ensure_schema_upgrades

    app = create_app()
    with app.app_context():
        keg = Keg(name="Fût inactif", volume_l=30.0)
        tap = Tap(number=504, name="Tireuse inactive", campus="brest")
        db.session.add_all([keg, tap])
        db.session.flush()
        tap.keg_id = keg.id
        article = Article(
            name="Pinte de tireuse inactive",
            article_type="biere",
            volume_cl=50,
            is_alcohol=True,
            is_tap=True,
            tap_number=tap.number,
            keg_id=keg.id,
            campus="brest",
            price_std=200,
            active=False,
        )
        db.session.add(article)
        db.session.commit()
        keg_id, article_id = keg.id, article.id

        columns = {c["name"] for c in sqla_inspect(db.engine).get_columns("kegs")}
        if "active" not in columns:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE kegs ADD COLUMN active BOOLEAN NOT NULL DEFAULT 1"))
        with db.engine.begin() as conn:
            conn.execute(text("UPDATE kegs SET active = 0 WHERE id = :id"), {"id": keg_id})

        ensure_schema_upgrades()
        columns = {c["name"] for c in sqla_inspect(db.engine).get_columns("kegs")}
        _expect("active" not in columns, "colonne active retirée")
        db.session.expire_all()
        _expect(
            db.session.get(Article, article_id).active is True,
            "articles d'une tireuse montée réactivés",
        )


def test_schema_upgrade_drops_keg_active_ignore_les_anciens_articles():
    if not IS_SQLITE:
        print("  (ignoré : évolution de schéma SQLite)")
        return
    from app import ensure_schema_upgrades
    from app.services.catalog import assign_keg

    app = create_app()
    with app.app_context():
        keg = Keg(name="Fût réactivé", volume_l=30.0)
        tap = Tap(number=508, name="Tireuse réactivée", campus="brest")
        db.session.add_all([keg, tap])
        db.session.flush()
        row = keg.price_row("brest")
        row.price_half_std = 200
        row.price_pint_std = 400
        row.price_pot_std = 300
        db.session.commit()
        # Deux régénérations successives : le premier jeu d'articles reste en
        # base, inactif, avec le même tap_number et le même keg_id.
        assign_keg(tap, keg)
        assign_keg(tap, keg)
        articles = db.session.scalars(
            select(Article).where(
                Article.tap_number == tap.number,
                Article.is_tap.is_(True),
            )
        ).all()
        _expect(len(articles) == 2 * len(TAP_SIZES), "deux jeux d'articles en base")
        latest_ids = {
            max(
                (a for a in articles if a.volume_cl == volume),
                key=lambda a: a.id,
            ).id
            for _, volume in TAP_SIZES.values()
        }
        # Avant la révision 0016, un fût inactif laissait tous ces articles
        # désactivés, y compris le jeu courant.
        for a in articles:
            a.active = False
        db.session.commit()
        keg_id = keg.id

        columns = {c["name"] for c in sqla_inspect(db.engine).get_columns("kegs")}
        if "active" not in columns:
            with db.engine.begin() as conn:
                conn.execute(text("ALTER TABLE kegs ADD COLUMN active BOOLEAN NOT NULL DEFAULT 1"))
        with db.engine.begin() as conn:
            conn.execute(text("UPDATE kegs SET active = 0 WHERE id = :id"), {"id": keg_id})

        ensure_schema_upgrades()
        columns = {c["name"] for c in sqla_inspect(db.engine).get_columns("kegs")}
        _expect("active" not in columns, "colonne active retirée")
        db.session.expire_all()
        active_ids = set(
            db.session.scalars(
                select(Article.id).where(
                    Article.tap_number == tap.number,
                    Article.is_tap.is_(True),
                    Article.active.is_(True),
                )
            ).all()
        )
        _expect(active_ids == latest_ids, "seul le dernier article de chaque taille est réactivé")
        _expect(len(active_ids) == len(TAP_SIZES), "un seul jeu d'articles actif")


def test_schema_upgrade_campus_catalog():
    """Bascule « catalogues par campus » sur une base héritée : campus déduit
    du porteur, scission des articles standards partagés, rattachement des
    ventes parisiennes à la copie, retrait des colonnes historiques."""
    if not IS_SQLITE:
        print("  (ignoré : évolution de schéma SQLite)")
        return
    from app import ensure_schema_upgrades

    app = create_app()
    with app.app_context():
        shared = Article(name="Coca partagé", article_type="snack", campus="brest", active=True)
        paris_only = Article(
            name="Spéculoos paris", article_type="snack", campus="brest", active=True
        )
        db.session.add_all([shared, paris_only])
        db.session.commit()
        article_id, paris_only_id = shared.id, paris_only.id
        with db.engine.begin() as conn:
            # reconstitue une base héritée : prix par campus + article partagé
            for col in (
                "price_std_brest",
                "price_std_paris",
                "price_team_brest",
                "price_team_paris",
            ):
                conn.execute(
                    text(f"ALTER TABLE articles ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
                )
            conn.execute(
                text(
                    "UPDATE articles SET price_std = 0, price_team = 0, "
                    "price_std_brest = 100, price_team_brest = 80, "
                    "price_std_paris = 120, price_team_paris = 90 "
                    "WHERE id = :i"
                ),
                {"i": article_id},
            )
            conn.execute(
                text(
                    "INSERT INTO transactions (created_at, type, campus, total, operator_label, "
                    "deposit_glasses, cancelled) "
                    "VALUES (CURRENT_TIMESTAMP, 'achat', 'paris', 120, 'test', 0, 0)"
                )
            )
            txn_id = conn.execute(text("SELECT MAX(id) FROM transactions")).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO transaction_lines (transaction_id, article_id, article_name, "
                    "article_type, quantity, unit_price, line_total) "
                    "VALUES (:t, :a, 'Coca partagé', 'snack', 1, 120, 120)"
                ),
                {"t": txn_id, "a": article_id},
            )
            conn.execute(
                text(
                    "UPDATE articles SET price_std = 0, price_team = 0, "
                    "price_std_paris = 150 WHERE id = :i"
                ),
                {"i": paris_only_id},
            )
        ensure_schema_upgrades()
        db.session.expire_all()

        cols = {c["name"] for c in sqla_inspect(db.engine).get_columns("articles")}
        _expect(
            {
                "price_std_brest",
                "price_std_paris",
                "price_team_brest",
                "price_team_paris",
            }.isdisjoint(cols),
            "colonnes de prix par campus retirées",
        )
        _expect(
            "campus" in cols and "price_std" in cols and "price_team" in cols,
            "colonnes campus présentes",
        )
        shared = db.session.get(Article, article_id)
        _expect(
            shared.campus == "brest" and shared.price_std == 100 and shared.price_team == 80,
            "article partagé conservé côté brestois",
        )
        copies = (
            db.session.query(Article)
            .filter(Article.name == "Coca partagé", Article.campus == "paris")
            .all()
        )
        _expect(
            len(copies) == 1 and copies[0].price_std == 120 and copies[0].price_team == 90,
            "copie parisienne créée avec les prix parisiens",
        )
        line_campus = db.session.scalar(select(Transaction.campus).where(Transaction.id == txn_id))
        line_article_id = db.session.execute(
            text("SELECT article_id FROM transaction_lines WHERE transaction_id = :t"),
            {"t": txn_id},
        ).scalar()
        _expect(
            line_campus == "paris" and line_article_id == copies[0].id,
            "vente parisienne rattachée à la copie",
        )
        paris_only = db.session.get(Article, paris_only_id)
        _expect(
            paris_only.campus == "paris" and paris_only.price_std == 150,
            "article uniquement parisien basculé sur paris",
        )
        # deuxième passage : aucun changement ni erreur
        ensure_schema_upgrades()


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
