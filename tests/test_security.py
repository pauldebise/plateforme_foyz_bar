"""Tests de sécurité applicative : configuration, IP client, limiteur, FK SQLite.

Exécutable sans pytest : python -m tests.test_security

Couvre (phase 1 du plan d'action) :
- T-1.1 : refus des secrets par défaut en production, tolérance en développement ;
- T-1.3 : IP client fiable (X-Forwarded-For ignoré, CF-Connecting-IP conditionnel),
  limiteur non contournable et borné ;
- T-1.4 : clés étrangères SQLite appliquées, historique tolérant aux comptes
  supprimés.
"""

import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Environnement figé AVANT tout import de app.* : la configuration lit les
# variables à l'import du module (SECRET_KEY forte = production valide).
_TMP = Path(tempfile.mkdtemp(prefix="foyz_secu_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
IS_SQLITE = os.environ["DATABASE_URL"].startswith("sqlite")
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)
os.environ.pop("TRUSTED_PROXY", None)
os.environ.pop("PROXY_FIX_X_FOR", None)

from flask import Flask  # noqa: E402
from sqlalchemy import text  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.config import INSECURE_SECRET_KEYS, validate_config  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Article, Transaction, User  # noqa: E402
from app.routes import auth as auth_module  # noqa: E402
from app.services.transactions import create_purchase, describe_transaction  # noqa: E402
from app.utils import client_ip  # noqa: E402


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _config_app(**overrides):
    app = Flask("config-test")
    app.config.update(
        {
            "DEBUG": False,
            "SECRET_KEY": "a" * 32,
            "DEFAULT_ADMIN_PASSWORD": "mot-de-passe-solide",
            "SESSION_COOKIE_SECURE": True,
        }
    )
    app.config.update(overrides)
    return app


def _raises_runtime(fn, label):
    try:
        fn()
    except RuntimeError:
        return
    raise AssertionError(f"échec : {label} (aucune exception)")


def test_production_refuses_default_secrets():
    for bad in ["", "trop-court", *sorted(INSECURE_SECRET_KEYS)]:
        app = _config_app(SECRET_KEY=bad)
        _raises_runtime(
            lambda app=app: validate_config(app),
            f"SECRET_KEY refusée en production : {bad!r}",
        )
    for bad in ["", "admin"]:
        app = _config_app(DEFAULT_ADMIN_PASSWORD=bad)
        _raises_runtime(
            lambda app=app: validate_config(app),
            f"ADMIN_PASSWORD refusée en production : {bad!r}",
        )


def test_production_accepts_strong_secrets():
    validate_config(_config_app())


def test_development_tolerates_default_secrets():
    app = _config_app(
        DEBUG=True,
        SECRET_KEY="dev-secret-key-change-me",
        DEFAULT_ADMIN_PASSWORD="admin",
    )
    validate_config(app)


def test_production_warns_without_secure_cookies():
    app = _config_app(SESSION_COOKIE_SECURE=False)
    validate_config(app)


def test_client_ip_ignores_untrusted_headers():
    app = Flask("ip-test")
    with app.test_request_context(
        "/",
        headers={"X-Forwarded-For": "203.0.113.7", "CF-Connecting-IP": "203.0.113.8"},
        environ_base={"REMOTE_ADDR": "10.0.0.9"},
    ):
        _expect(client_ip() == "10.0.0.9", "en-têtes d'IP ignorés sans proxy de confiance")


def test_client_ip_uses_cf_only_when_trusted():
    app = Flask("ip-test")
    app.config["TRUSTED_PROXY"] = "cloudflare"
    with app.test_request_context(
        "/",
        headers={"CF-Connecting-IP": "203.0.113.8", "X-Forwarded-For": "1.2.3.4"},
        environ_base={"REMOTE_ADDR": "172.64.0.1"},
    ):
        _expect(client_ip() == "203.0.113.8", "CF-Connecting-IP retenu si TRUSTED_PROXY=cloudflare")
    with app.test_request_context(
        "/",
        headers={"CF-Connecting-IP": "pas-une-ip", "X-Forwarded-For": "1.2.3.4"},
        environ_base={"REMOTE_ADDR": "10.0.0.9"},
    ):
        _expect(client_ip() == "10.0.0.9", "CF-Connecting-IP invalide -> remote_addr")


def test_limiter_purges_and_bounds_keys():
    now = time.time()
    with auth_module._limiter_lock:
        auth_module._attempts.clear()
        for i in range(auth_module._RATE_MAX_KEYS + 5):
            auth_module._attempts[f"expired-{i}"].append(now - auth_module._RATE_WINDOW_SECONDS - 1)
        auth_module._last_sweep = 0.0
        auth_module._sweep_attempts(now)
        _expect(not auth_module._attempts, "clés expirées purgées")
        for i in range(auth_module._RATE_MAX_KEYS + 5):
            auth_module._attempts[f"active-{i}"].append(now - (i % 100))
        auth_module._last_sweep = 0.0
        auth_module._sweep_attempts(now)
        _expect(
            len(auth_module._attempts) <= auth_module._RATE_MAX_KEYS,
            "nombre de clés borné",
        )
        auth_module._attempts.clear()


def test_sqlite_foreign_keys_and_history_after_deletion():
    if not IS_SQLITE:
        print("  (ignoré : clés étrangères SQLite)")
        return
    app = create_app()
    with app.app_context():
        _expect(
            db.session.execute(text("PRAGMA foreign_keys")).scalar() == 1,
            "PRAGMA foreign_keys actif sur SQLite",
        )
        user = User(
            name="Élève Supprimé",
            username="eleve.supprime",
            password_hash=generate_password_hash("secret123"),
        )
        db.session.add(user)
        db.session.flush()
        user.wallet("brest").balance = 500
        article = Article(
            name="Pinte test",
            article_type="biere",
            is_alcohol=True,
            campus="brest",
            price_std=250,
            price_team=250,
            active=True,
        )
        db.session.add(article)
        db.session.commit()

        transaction = create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article.id, "quantity": 1}],
            contributor_ids=[user.id],
        )
        transaction_id, user_id = transaction.id, user.id

        db.session.delete(user)
        db.session.commit()
        db.session.expire_all()

        remaining = db.session.get(Transaction, transaction_id)
        _expect(
            all(c.user_id is None for c in remaining.contributions),
            "contributions détachées par ON DELETE SET NULL",
        )
        describe_transaction(remaining)  # ne doit pas lever

        # orphelin hérité (compte supprimé avant l'activation des FK SQLite)
        raw = sqlite3.connect(db.engine.url.database)
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute(
            "INSERT INTO contributions (transaction_id, user_id, campus, amount, balance_after) "
            "VALUES (?, ?, 'brest', -100, 0)",
            (transaction_id, user_id),
        )
        raw.commit()
        raw.close()
        db.session.expire_all()
        orphaned = db.session.get(Transaction, transaction_id)
        _expect(
            "Compte supprimé" in describe_transaction(orphaned),
            "orphelin hérité affiché sans erreur",
        )


def test_login_limiter_survives_forged_xff():
    app = create_app()
    client = app.test_client()
    html = client.get("/connexion").get_data(as_text=True)
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    _expect(match is not None, "jeton CSRF présent")
    token = match.group(1)
    status = None
    for i in range(auth_module._RATE_MAX_ATTEMPTS + 1):
        status = client.post(
            "/connexion",
            data={
                "username": "inconnu",
                "password": "mauvais",
                "campus": "brest",
                "_csrf": token,
            },
            headers={"X-Forwarded-For": f"203.0.113.{i}"},
            environ_base={"REMOTE_ADDR": "198.51.100.77"},
        ).status_code
    _expect(
        status == 429,
        f"9e tentative bloquée malgré un X-Forwarded-For forgé (statut {status})",
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
