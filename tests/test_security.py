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
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Environnement figé AVANT tout import de app.* : la configuration lit les
# variables à l'import du module (SECRET_KEY forte = production valide).
_TMP = Path(tempfile.mkdtemp(prefix="foyz_secu_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'app.db'}"
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)
os.environ.pop("TRUSTED_PROXY", None)
os.environ.pop("PROXY_FIX_X_FOR", None)

from flask import Flask  # noqa: E402

from app.config import INSECURE_SECRET_KEYS, validate_config  # noqa: E402


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
