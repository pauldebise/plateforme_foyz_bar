"""Tests d'exploitation (phase 6) : supervision, journaux, permissions, purge.

Exécutable sans pytest : python -m tests.test_monitoring

Couvre :
- T-6.2 : base SQLite en 600 (dossier `instance/` en 700), purge du registre
  des connexions (CLI et déclenchement sur échec de connexion, R13) ;
- T-6.3 : `/health` (base + uploads), page 500 journalisée en événement
  structuré sans donnée sensible, formateur JSON.
"""

import json
import logging
import os
import re
import stat
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_monitoring_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
IS_SQLITE = os.environ["DATABASE_URL"].startswith("sqlite")
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from app import _restrict_instance_permissions, create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.logging_setup import JsonFormatter  # noqa: E402
from app.models import LoginLog  # noqa: E402
from app.utils import utcnow  # noqa: E402
import app.routes.auth as auth  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(html):
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    _expect(match is not None, "jeton CSRF présent")
    return match.group(1)


def _login(client, username, password, campus="brest"):
    html = client.get("/connexion").get_data(as_text=True)
    token = _csrf(html)
    return client.post(
        "/connexion",
        data={"username": username, "password": password, "campus": campus, "_csrf": token},
    )


class _Collector(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_health_endpoint():
    app = create_app()
    client = app.test_client()
    res = client.get("/health")
    payload = res.get_json()
    _expect(res.status_code == 200, f"health 200 ({res.status_code})")
    _expect(payload["status"] == "ok", "statut global ok")
    _expect(payload["database"] == "ok", "base joignable")
    _expect(payload["uploads"] == "ok", "uploads accessibles")
    _expect("schema" in payload, "version de schéma rapportée")


def test_health_degraded_when_uploads_unavailable():
    app = create_app()
    blocker = _TMP / "fichier-bloquant"
    blocker.write_text("x", encoding="utf-8")
    previous = app.config["UPLOAD_FOLDER"]
    app.config["UPLOAD_FOLDER"] = str(blocker / "sous-dossier")
    try:
        res = app.test_client().get("/health")
        _expect(res.status_code == 503, f"health 503 si uploads indisponibles ({res.status_code})")
        _expect(res.get_json()["uploads"] == "error", "uploads signalés en erreur")
    finally:
        app.config["UPLOAD_FOLDER"] = previous


def test_health_degraded_when_database_down():
    from unittest import mock

    from sqlalchemy import create_engine

    app = create_app()
    broken = create_engine("sqlite:////dossier-inexistant/base.db")
    with mock.patch.object(type(db), "engine", new=property(lambda self: broken), create=True):
        res = app.test_client().get("/health")
    _expect(res.status_code == 503, f"health 503 si base injoignable ({res.status_code})")
    _expect(res.get_json()["database"] == "error", "base signalée en erreur")


def test_500_handler_logs_structured_event_without_secret():
    app = create_app()
    collector = _Collector()
    app.logger.addHandler(collector)

    def boom():
        raise ValueError("motdepasse-tres-secret-a-ne-pas-journaliser")

    app.add_url_rule("/boom-test", "boom_test", boom)
    try:
        res = app.test_client().get("/boom-test")
    finally:
        app.logger.removeHandler(collector)

    _expect(res.status_code == 500, f"page 500 servie ({res.status_code})")
    events = [r for r in collector.records if getattr(r, "event", None) == "unhandled_error"]
    _expect(events, "événement unhandled_error journalisé")
    payload = json.loads(JsonFormatter().format(events[0]))
    _expect(payload["message"] == "unhandled_error", "message structuré sans valeur métier")
    _expect(payload["path"] == "/boom-test", "chemin journalisé")
    _expect("motdepasse-tres-secret" not in payload["message"], "message sans secret")
    _expect("exception" in payload, "trace d'erreur jointe pour l'alerte")


def test_json_formatter_parses_and_keeps_extras():
    record = logging.LogRecord("test", logging.WARNING, __file__, 1, "coucou %s", ("x",), None)
    record.event = "unhandled_error"
    record.path = "/ici"
    payload = json.loads(JsonFormatter().format(record))
    _expect(payload["level"] == "WARNING", "niveau conservé")
    _expect(payload["message"] == "coucou x", "message formaté")
    _expect(
        payload["event"] == "unhandled_error" and payload["path"] == "/ici",
        "champs structurés conservés",
    )


def test_sqlite_permissions_restricted():
    if not IS_SQLITE:
        print("  (ignoré : permissions de fichier SQLite)")
        return
    app = create_app()
    db_path = _TMP / "app.db"
    _restrict_instance_permissions(app)
    _expect(stat.S_IMODE(db_path.stat().st_mode) == 0o600, "base SQLite en 600")

    elsewhere = _TMP / "partage"
    elsewhere.mkdir(exist_ok=True)
    os.chmod(elsewhere, 0o755)
    other_db = elsewhere / "other.db"
    other_db.touch()
    previous = app.config["SQLALCHEMY_DATABASE_URI"]
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{other_db}"
    try:
        _restrict_instance_permissions(app)
    finally:
        app.config["SQLALCHEMY_DATABASE_URI"] = previous
    _expect(stat.S_IMODE(other_db.stat().st_mode) == 0o600, "fichier durci")
    _expect(
        stat.S_IMODE(elsewhere.stat().st_mode) == 0o755,
        "dossier parent hors instance/ jamais modifié",
    )


def test_purge_logs_command():
    app = create_app()
    with app.app_context():
        old = LoginLog(
            name="ancien",
            campus="brest",
            ip="127.0.0.1",
            success=False,
            created_at=utcnow() - timedelta(days=200),
        )
        recent = LoginLog(
            name="recent", campus="brest", ip="127.0.0.1", success=True, created_at=utcnow()
        )
        db.session.add_all([old, recent])
        db.session.commit()
        old_id = old.id

    runner = app.test_cli_runner()
    result = runner.invoke(args=["purge-logs", "--days", "90", "--dry-run"])
    _expect(result.exit_code == 0, "purge-logs --dry-run")
    with app.app_context():
        _expect(db.session.get(LoginLog, old_id) is not None, "dry-run ne supprime rien")

    result = runner.invoke(args=["purge-logs", "--days", "90"])
    _expect(result.exit_code == 0, "purge-logs")
    with app.app_context():
        _expect(db.session.get(LoginLog, old_id) is None, "entrée ancienne supprimée")
        _expect(db.session.scalars(db.select(LoginLog)).all(), "entrée récente conservée")


def test_failed_login_still_triggers_cleanup():
    app = create_app()
    with app.app_context():
        old = LoginLog(
            name="tres ancien",
            campus="brest",
            ip="127.0.0.1",
            success=False,
            created_at=utcnow() - timedelta(days=400),
        )
        db.session.add(old)
        db.session.commit()
        old_id = old.id

    auth._last_log_cleanup = -10_000.0
    client = app.test_client()
    res = _login(client, "inconnu", "mauvais")
    _expect(res.status_code == 401, f"échec de connexion attendu ({res.status_code})")
    with app.app_context():
        _expect(
            db.session.get(LoginLog, old_id) is None,
            "purge déclenchée même sans connexion réussie (R13)",
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
    raise SystemExit(main())
