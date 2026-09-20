"""Tests du tableau de bord « santé » (P10-2).

Exécutable sans pytest : python -m tests.test_sante

Couvre :
- page /admin/sante : état base/uploads/schéma, version, activité, disques,
  sauvegardes ; accès réservé aux mandats ;
- service app/services/health.py : inventaire des sauvegardes (chiffrement,
  hebdomadaires, fichiers étrangers ignorés), espace disque, taille de base,
  cohérence avec /health.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_sante_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
IS_SQLITE = os.environ["DATABASE_URL"].startswith("sqlite")
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import User  # noqa: E402
from app.services import health as H  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(client, path="/connexion"):
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    _expect(match is not None, f"jeton CSRF présent sur {path}")
    return match.group(1)


def _login(client, username="admin", password=ADMIN_PASSWORD):
    token = _csrf(client)
    return client.post(
        "/connexion",
        data={"username": username, "password": password, "campus": "brest", "_csrf": token},
    )


def test_page_sante():
    app = create_app()
    client = app.test_client()
    _expect(_login(client).status_code == 302, "connexion administrateur")
    res = client.get("/admin/sante")
    _expect(res.status_code == 200, f"page santé servie ({res.status_code})")
    page = res.get_data(as_text=True)
    for marker in (
        "Santé de la plateforme",
        "Base de données",
        "Schéma de base",
        "Version applicative",
        "Espace disque",
        "Sauvegardes",
    ):
        _expect(marker in page, f"section « {marker} » présente")


def test_sante_reservee_aux_mandats():
    app = create_app()
    with app.app_context():
        old = User(
            name="Ancien Sante",
            username="ancien.sante",
            team_status="ancien",
            team_campus="brest",
            password_hash=generate_password_hash("secret123"),
        )
        db.session.add(old)
        db.session.commit()
    client = app.test_client()
    _expect(_login(client, "ancien.sante", "secret123").status_code == 302, "connexion ancien")
    _expect(client.get("/admin/sante").status_code == 403, "santé interdite aux anciens membres")


def test_system_checks_and_health_agree():
    app = create_app()
    with app.app_context():
        checks = H.system_checks()
        _expect(checks["database"] == "ok", "base joignable")
        _expect(checks["uploads"] == "ok", "uploads accessibles")
        _expect(checks["schema"] == "ok", "schéma à jour")
        info = H.disk_info(app.config["UPLOAD_FOLDER"])
        _expect(info and info["total"] > 0 and 0 <= info["percent"] <= 100, "espace disque")
        size = H.database_size()
        if IS_SQLITE:
            _expect(size and size > 0, "taille du fichier SQLite")
        else:
            _expect(size is None, "pas de fichier SQLite en PostgreSQL")
    payload = app.test_client().get("/health").get_json()
    _expect(payload["status"] == "ok", "sonde /health cohérente")


def test_backups_info():
    app = create_app()
    directory = Path(tempfile.mkdtemp(prefix="foyz_sante_backups_"))
    stamp = "20260917-120000"
    (directory / f"foyz-{stamp}.dump.gpg").write_bytes(b"chiffre")
    (directory / f"uploads-{stamp}.tgz").write_bytes(b"archive")
    (directory / "a-lire.txt").write_text("hors format", encoding="utf-8")
    weekly = directory / "weekly"
    weekly.mkdir()
    (weekly / "foyz-20260910-120000.dump").write_bytes(b"hebdo")
    old = 1_700_000_000
    os.utime(directory / f"foyz-{stamp}.dump.gpg", (old, old))

    with app.app_context():
        app.config["BACKUP_DIR"] = directory
        info = H.backups_info()
        _expect(info["exists"], "dossier détecté")
        _expect(info["count"] == 3, f"trois sauvegardes inventoriées ({info['count']})")
        by_name = {entry["name"]: entry for entry in info["entries"]}
        _expect(by_name[f"foyz-{stamp}.dump.gpg"]["encrypted"], "chiffrement signalé")
        _expect(not by_name[f"uploads-{stamp}.tgz"]["encrypted"], "clair signalé")
        _expect(by_name["foyz-20260910-120000.dump"]["weekly"], "copie hebdo signalée")
        _expect(info["total_size"] > 0, "taille cumulée")
        _expect(
            not any(entry["name"] == "a-lire.txt" for entry in info["entries"]),
            "fichier étranger ignoré",
        )
        _expect(
            info["entries"][-1]["name"] == f"foyz-{stamp}.dump.gpg",
            "tri par date (plus ancienne en dernier)",
        )
    with app.app_context():
        app.config["BACKUP_DIR"] = directory / "absent"
        _expect(H.backups_info()["count"] == 0, "dossier absent géré")
        _expect(not H.backups_info()["exists"], "absence signalée")


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
