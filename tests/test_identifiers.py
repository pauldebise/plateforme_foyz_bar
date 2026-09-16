"""Tests applicatifs : identifiant de connexion (username), surnom, évolution du schéma.

Exécutable sans pytest : python -m tests.test_identifiers

Couvre :
- le slug d'identifiant (app.utils.slug_username) et la propriété display_name ;
- la connexion équipe (username prenom.nom, tolérant casse/accents/espaces ;
  le surnom n'est PAS un identifiant) ;
- la création/édition de comptes en admin (unicité username, recherche par
  surnom) ;
- ensure_schema_upgrades sur une base héritée (ajout colonnes, backfill
  dédoublonné, bascule d'unicité name -> username), avec idempotence.
"""

import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Environnement figé AVANT tout import de app.* : la configuration lit les
# variables d'authentification à l'import du module.
_TMP = Path(tempfile.mkdtemp(prefix="foyz_ident_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'app.db'}"
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from sqlalchemy import inspect, select, text  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app, ensure_schema_upgrades  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import User  # noqa: E402
from app.utils import slug_username  # noqa: E402

ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(client, path="/connexion"):
    html = client.get(path).get_data(as_text=True)
    m = re.search(r'name="_csrf" value="([^"]+)"', html)
    _expect(m is not None, f"jeton CSRF présent sur {path}")
    return m.group(1)


def test_slug_username_and_display_name():
    _expect(slug_username("Paul Debise") == "paul.debise", "slug simple")
    _expect(slug_username("Marie Claire Dupont") == "marie.claire.dupont",
            "slug multi-mots")
    _expect(slug_username("  ÉLÈVE Dupont ") == "eleve.dupont", "slug accents")
    _expect(slug_username("") is None and slug_username(None) is None,
            "slug vide -> None")
    u = User(name="Paul Debise", nickname="Chips")
    _expect(u.display_name == "Paul Debise (Chips)", "display_name avec surnom")
    _expect(User(name="Marie Le Goff").display_name == "Marie Le Goff",
            "display_name sans surnom")


def test_login_and_account_management():
    app = create_app()
    client = app.test_client()
    token = _csrf(client)

    # login admin : casse et espaces tolérés ("ADMIN" -> "admin")
    r = client.post("/connexion", data={
        "username": "ADMIN", "password": ADMIN_PASSWORD,
        "campus": "brest", "_csrf": token,
    })
    _expect(r.status_code == 302, f"login admin accepté ({r.status_code})")
    # session.clear() au login régénère le jeton CSRF
    token = _csrf(client, "/admin/comptes")

    # création de compte : identifiant déduit du nom si laissé vide
    r = client.post("/admin/comptes/nouveau", data={
        "name": "Paul Debise", "nickname": "Chips",
        "username": "", "promotion": "2028", "_csrf": token,
    })
    _expect(r.status_code == 302, f"compte créé ({r.status_code})")
    with app.app_context():
        u = db.session.scalars(
            select(User).where(User.username == "paul.debise")
        ).first()
        _expect(u is not None and u.name == "Paul Debise" and u.nickname == "Chips",
                "compte créé avec identifiant déduit du nom et surnom conservé")

    # unicité de l'identifiant (doublon refusé)
    r = client.post("/admin/comptes/nouveau", data={
        "name": "Paul Debi", "username": "paul.debise", "_csrf": token,
    }, follow_redirects=True)
    _expect("déjà utilisé" in r.get_data(as_text=True), "doublon d'identifiant refusé")

    # recherche de comptes par surnom
    r = client.get("/admin/comptes?q=Chips")
    body = r.get_data(as_text=True)
    _expect("Paul Debise" in body and "paul.debise" in body,
            "recherche par surnom + affichage identifiant")

    # édition : renommage sans unicité sur le nom, contrôle sur l'identifiant
    r = client.post(f"/admin/comptes/{u.id}", data={
        "name": "Paul Debise", "nickname": "Chips", "username": "paul.debi",
        "promotion": "2028", "_csrf": token,
    }, follow_redirects=True)
    with app.app_context():
        db.session.expire_all()
        u = db.session.get(User, u.id)
        _expect(u.username == "paul.debi", f"identifiant renommé ({u.username})")
        # remise en état pour la suite du test
        u.username = "paul.debise"
        u.password_hash = generate_password_hash("secret123")
        u.team_status = "mandat"
        u.team_campus = "brest"
        db.session.commit()

    client.post("/deconnexion", data={"_csrf": token})
    token = _csrf(client)  # logout -> session.clear() -> nouveau jeton

    # login équipe : accents/casse/espaces tolérés, le surnom n'est PAS un identifiant
    r = client.post("/connexion", data={
        "username": "paul debise", "password": "secret123",
        "campus": "brest", "_csrf": token,
    })
    _expect(r.status_code == 302, f"login équipe par prenom.nom ({r.status_code})")
    token = _csrf(client)  # session.clear() au login -> nouveau jeton
    r = client.post("/connexion", data={
        "username": "paul debise", "password": "mauvais",
        "campus": "brest", "_csrf": token,
    })
    _expect(r.status_code == 401, "mauvais mot de passe refusé")
    r = client.post("/connexion", data={
        "username": "Chips", "password": "secret123",
        "campus": "brest", "_csrf": token,
    })
    _expect(r.status_code == 401, "le surnom ne permet pas de se connecter")


_LEGACY_SCHEMA = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY,
  name VARCHAR(255) NOT NULL,
  promotion INTEGER,
  password_hash VARCHAR(255),
  legacy_password VARCHAR(255),
  team_status VARCHAR(10),
  team_campus VARCHAR(10),
  blacklist BOOLEAN,
  blacklist_alcohol BOOLEAN,
  blacklist_reason VARCHAR(255),
  created_at DATETIME
);
CREATE UNIQUE INDEX ix_users_name ON users (name);
"""


def test_schema_upgrade_legacy_db():
    """Base héritée (sans username/nickname, unicité sur name) : ajout des
    colonnes, backfill dédoublonné, bascule d'unicité, le tout idempotent."""
    db_path = Path(tempfile.mkdtemp(prefix="foyz_legacy_")) / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.executemany(
        "INSERT INTO users (name, created_at) VALUES (?, '2026-01-01 00:00:00')",
        [("Paul Debise",), ("Marie Le Goff",), ("admin",), ("Paul  Debise",)],
    )
    conn.commit()
    conn.close()

    from flask import Flask

    app = Flask("legacy")
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        ensure_schema_upgrades()
        cols = {c["name"] for c in inspect(db.engine).get_columns("users")}
        _expect("username" in cols and "nickname" in cols, "colonnes ajoutées")
        mapping = dict(db.session.execute(
            text("SELECT name, username FROM users")
        ).fetchall())
        _expect(mapping["Paul Debise"] == "paul.debise", "backfill slug simple")
        _expect(mapping["Marie Le Goff"] == "marie.le.goff", "backfill slug multi-mots")
        _expect(mapping["admin"] == "admin", "backfill identifiant déjà conforme")
        _expect(mapping["Paul  Debise"] == "paul.debise2",
                f"collision de slug dédoublonnée ({mapping['Paul  Debise']})")
        indexes = {ix["name"]: ix for ix in inspect(db.engine).get_indexes("users")}
        _expect(not indexes["ix_users_name"]["unique"], "unicité retirée de name")
        _expect(indexes["ix_users_username"]["unique"], "unicité posée sur username")
        # deuxième passage : aucun changement ni erreur
        ensure_schema_upgrades()
        mapping2 = dict(db.session.execute(
            text("SELECT name, username FROM users")
        ).fetchall())
        _expect(mapping2 == mapping, "upgrade idempotent")


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
