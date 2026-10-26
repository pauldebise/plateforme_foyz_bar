"""Décor de Noël : fenêtre annuelle, inclusion du script et réglage superadmin.

Exécutable sans pytest : python -m tests.test_deco_noel
"""

import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_deco_noel_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import AuditLog, Setting, User  # noqa: E402
from app.services import christmas  # noqa: E402
from app.services.settings import get_setting, set_setting  # noqa: E402
from app.utils import PARIS_TZ  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
_state = {}


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


def _setup():
    if _state:
        return _state
    app = create_app()
    with app.app_context():
        mandat = User(
            name="Mandat Noël",
            username="mandat.noel",
            team_status="mandat",
            team_campus="brest",
            password_hash=generate_password_hash("secret123"),
        )
        db.session.add(mandat)
        db.session.commit()
    _state["app"] = app
    return _state


def _set_dates(app, start, end):
    with app.app_context():
        set_setting("christmas_deco_start", start)
        set_setting("christmas_deco_end", end)
        db.session.commit()


def _reset_dates(app):
    with app.app_context():
        for key in ("christmas_deco_start", "christmas_deco_end"):
            row = db.session.get(Setting, key)
            if row is not None:
                db.session.delete(row)
        db.session.commit()


def test_dates_par_defaut():
    app = _setup()["app"]
    _reset_dates(app)
    with app.app_context():
        _expect(christmas.is_active(date(2026, 12, 1)), "1er décembre actif")
        _expect(christmas.is_active(date(2026, 12, 31)), "31 décembre actif")
        _expect(not christmas.is_active(date(2026, 11, 30)), "30 novembre inactif")
        _expect(not christmas.is_active(date(2027, 1, 1)), "1er janvier inactif")


def test_fenetre_configuree_et_chevauchement():
    app = _setup()["app"]
    _set_dates(app, "11-15", "01-15")
    with app.app_context():
        _expect(christmas.is_active(date(2026, 11, 15)), "début inclus")
        _expect(christmas.is_active(date(2026, 12, 25)), "Noël actif")
        _expect(christmas.is_active(date(2027, 1, 15)), "fin incluse")
        _expect(not christmas.is_active(date(2026, 11, 14)), "avant le début")
        _expect(not christmas.is_active(date(2027, 1, 16)), "après la fin")
        _expect(
            christmas.date_input_values("11-28", "01-05", year=2026)
            == ("2026-11-28", "2027-01-05"),
            "fin affichée l'année suivante quand la fenêtre chevauche le Nouvel An",
        )
    _expect(christmas.parse_form_date("2026-12-01") == "12-01", "date ISO convertie")
    _expect(christmas.parse_form_date("2026-02-30") is None, "date impossible refusée")
    _expect(christmas.parse_form_date("n'importe quoi") is None, "saisie libre refusée")


def test_dates_invalides():
    app = _setup()["app"]
    _set_dates(app, "13-01", "12-31")
    with app.app_context():
        _expect(not christmas.is_active(date(2026, 12, 25)), "mois invalide inactif")
    _set_dates(app, "", "")
    with app.app_context():
        _expect(not christmas.is_active(date(2026, 12, 25)), "dates vides inactives")


def test_page_publique_activee():
    app = _setup()["app"]
    today = datetime.now(PARIS_TZ).date()
    month_day = f"{today.month:02d}-{today.day:02d}"
    _set_dates(app, month_day, month_day)
    html = app.test_client().get("/").get_data(as_text=True)
    _expect("js/santa-deco.js" in html, "script du décor inclus dans la page publique")


def test_page_publique_hors_periode():
    app = _setup()["app"]
    today = datetime.now(PARIS_TZ).date()
    if (today.month, today.day) == (12, 31):
        start, end = "01-01", "01-02"
    else:
        start, end = "12-31", "12-31"
    _set_dates(app, start, end)
    html = app.test_client().get("/").get_data(as_text=True)
    _expect("js/santa-deco.js" not in html, "script absent hors période")


def test_acces_reserve_au_superadmin():
    app = _setup()["app"]
    client = app.test_client()
    _expect(
        _login(client, "mandat.noel", "secret123").status_code == 302,
        "connexion mandat",
    )
    page = client.get("/admin/module-dev").get_data(as_text=True)
    _expect("Décor de Noël" not in page, "réglage masqué pour un mandat")
    token = _csrf(client, "/admin/module-dev")
    res = client.post(
        "/admin/module-dev/deco-noel",
        data={
            "christmas_deco_start": "2026-12-01",
            "christmas_deco_end": "2026-12-31",
            "_csrf": token,
        },
    )
    _expect(res.status_code == 403, f"enregistrement interdit au mandat ({res.status_code})")


def test_enregistrement_et_audit():
    app = _setup()["app"]
    client = app.test_client()
    _expect(_login(client).status_code == 302, "connexion administrateur")
    page = client.get("/admin/module-dev").get_data(as_text=True)
    _expect("Décor de Noël" in page, "réglage visible pour le superadmin")
    _expect('name="christmas_deco_start"' in page, "champ de début présent")
    token = _csrf(client, "/admin/module-dev")
    res = client.post(
        "/admin/module-dev/deco-noel",
        data={
            "christmas_deco_start": "2026-11-28",
            "christmas_deco_end": "2027-01-05",
            "_csrf": token,
        },
        follow_redirects=True,
    )
    _expect(res.status_code == 200, "retour sur le module dev")
    with app.app_context():
        _expect(get_setting("christmas_deco_start") == "11-28", "début enregistré")
        _expect(get_setting("christmas_deco_end") == "01-05", "fin enregistrée")
        row = (
            db.session.query(AuditLog)
            .filter(AuditLog.action == "reglages.deco_noel")
            .order_by(AuditLog.id.desc())
            .first()
        )
        _expect(row is not None and "11-28" in row.details, "changement journalisé")
    year = datetime.now(PARIS_TZ).year
    page = client.get("/admin/module-dev").get_data(as_text=True)
    _expect(f'value="{year}-11-28"' in page, "champ début prérempli")
    _expect(f'value="{year + 1}-01-05"' in page, "champ fin prérempli")


def test_validation_et_desactivation():
    app = _setup()["app"]
    _set_dates(app, "12-01", "12-31")
    client = app.test_client()
    _expect(_login(client).status_code == 302, "connexion administrateur")
    token = _csrf(client, "/admin/module-dev")

    # Une seule date ou une date impossible : refus, ancienne valeur conservée.
    for start, end in (("2026-12-02", ""), ("2026-02-30", "2026-12-31")):
        client.post(
            "/admin/module-dev/deco-noel",
            data={
                "christmas_deco_start": start,
                "christmas_deco_end": end,
                "_csrf": token,
            },
        )
        with app.app_context():
            _expect(get_setting("christmas_deco_start") == "12-01", "début inchangé")
            _expect(get_setting("christmas_deco_end") == "12-31", "fin inchangée")

    # Deux champs vides : décoration désactivée.
    client.post(
        "/admin/module-dev/deco-noel",
        data={"christmas_deco_start": "", "christmas_deco_end": "", "_csrf": token},
    )
    with app.app_context():
        _expect(get_setting("christmas_deco_start") == "", "début effacé")
        _expect(get_setting("christmas_deco_end") == "", "fin effacée")
    html = app.test_client().get("/").get_data(as_text=True)
    _expect("js/santa-deco.js" not in html, "décor désactivé sur la page publique")


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
