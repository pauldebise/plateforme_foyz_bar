"""Robustesse des saisies : aucune valeur extrême ne doit provoquer d'erreur 500.

Exécutable sans pytest : python -m tests.test_limites_champs
(significatif surtout sur PostgreSQL : SQLite ne contrôle ni la longueur des
VARCHAR, ni la taille des entiers, ni les octets nuls).

Chaque formulaire, l'API de caisse, les filtres des listes et la connexion
reçoivent, champ par champ, des valeurs très longues, des octets nuls, des
entiers hors bornes, des chiffres exotiques (« ² ») et des valeurs non
numériques. Une réponse 4xx/3xx est un refus métier acceptable ; 5xx est un bug.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_limites_"))
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
from app.models import Article, Event, Keg, Tap, User  # noqa: E402
from datetime import timedelta  # noqa: E402

from app.utils import utcnow  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
LONG = "é" * 600
BAD = [
    LONG,
    "a\x00b",
    "9" * 30,
    "²",
    "٣",
    "-5",
    "1e999",
    "nan",
    "' OR 1=1 --",
    "<script>alert(1)</script>",
    " ",
    "0",
]
JSON_BAD = [LONG, "a\x00b", 10**30, -(10**30), "²", None, [], {}, [None], [10**30], [LONG], True]

_state = {}


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _setup():
    if _state:
        return _state
    app = create_app()
    with app.app_context():
        student = User(
            name="Fuzz Etudiant",
            username="fuzz.etudiant",
            promotion=2027,
            password_hash=generate_password_hash("secret123"),
        )
        target = User(
            name="Fuzz Cible",
            username="fuzz.cible",
            team_status="mandat",
            team_campus="brest",
            password_hash=generate_password_hash("secret123"),
        )
        article = Article(name="Fuzz Biere", article_type="biere", campus="brest", price_std=100)
        keg = Keg(name="Fuzz Fut", volume_l=30)
        db.session.add_all([student, target, article, keg])
        db.session.flush()
        student.wallet("brest").balance = 100_000
        tap = Tap(name="Fuzz Tireuse", number=900, campus="brest")
        event = Event(
            name="Fuzz Evenement",
            campus="brest",
            starts_at=utcnow(),
            ends_at=utcnow(),
            token="fuzz-token",
        )
        running = Event(
            name="Fuzz Passerelle",
            campus="brest",
            starts_at=utcnow() - timedelta(hours=1),
            ends_at=utcnow() + timedelta(hours=2),
            token="fuzz-passerelle",
        )
        db.session.add_all([tap, event, running])
        db.session.commit()
        _state.update(
            student=student.id,
            target=target.id,
            article=article.id,
            keg=keg.id,
            tap=tap.id,
            event=event.id,
            app=app,
        )
    client = app.test_client()
    html = client.get("/connexion").get_data(as_text=True)
    token = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    res = client.post(
        "/connexion",
        data={"username": "admin", "password": ADMIN_PASSWORD, "campus": "brest", "_csrf": token},
    )
    _expect(res.status_code == 302, "connexion administrateur")
    page = client.get("/admin/comptes").get_data(as_text=True)
    _state["client"] = client
    _state["csrf"] = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
    return _state


def _fuzz_form(path, defaults):
    """Poste `defaults` en remplaçant tour à tour chaque champ par chaque valeur extrême."""
    s = _setup()
    errors = []
    for field in defaults:
        for value in BAD:
            data = {**defaults, field: value, "_csrf": s["csrf"]}
            try:
                status = s["client"].post(path, data=data).status_code
            except Exception as exc:  # erreur non gérée remontée par le client de test
                status = f"exception {type(exc).__name__}: {str(exc)[:80]}"
            if not isinstance(status, int) or status >= 500:
                errors.append(f"{field}={value[:12]!r}… -> {status}")
    _expect(not errors, f"POST {path} : {len(errors)} erreur(s) 5xx, ex. {errors[:3]}")


def _fuzz_json(path, defaults):
    s = _setup()
    errors = []
    for field in defaults:
        for value in JSON_BAD:
            try:
                status = (
                    s["client"]
                    .post(path, json={**defaults, field: value}, headers={"X-CSRFToken": s["csrf"]})
                    .status_code
                )
            except Exception as exc:
                status = f"exception {type(exc).__name__}: {str(exc)[:80]}"
            if not isinstance(status, int) or status >= 500:
                errors.append(f"{field}={str(value)[:12]!r}… -> {status}")
    _expect(not errors, f"POST {path} (JSON) : {len(errors)} erreur(s) 5xx, ex. {errors[:3]}")


def _fuzz_get(path, params):
    s = _setup()
    errors = []
    for field in params:
        for value in BAD:
            try:
                status = s["client"].get(path, query_string={**params, field: value}).status_code
            except Exception as exc:
                status = f"exception {type(exc).__name__}: {str(exc)[:80]}"
            if not isinstance(status, int) or status >= 500:
                errors.append(f"{field}={value[:12]!r}… -> {status}")
    _expect(not errors, f"GET {path} : {len(errors)} erreur(s) 5xx, ex. {errors[:3]}")


def test_comptes():
    s = _setup()
    _fuzz_form(
        "/admin/comptes/nouveau",
        {"name": "Fuzz Nouveau", "nickname": "n", "username": "fuzz.nouveau", "promotion": "2027"},
    )
    _fuzz_form(
        f"/admin/comptes/{s['target']}",
        {
            "name": "Fuzz Cible",
            "nickname": "n",
            "username": "fuzz.cible",
            "promotion": "2027",
            "blacklist_reason": "motif",
            "blacklist_alcohol_reason": "motif alcool",
        },
    )
    _fuzz_form(
        f"/admin/comptes/{s['target']}/arbitrer-solde",
        {"amount": "1", "direction": "credit", "admin_password": ADMIN_PASSWORD},
    )
    _fuzz_form(
        f"/admin/equipe/{s['target']}",
        {"team_status": "mandat", "campus": "brest", "password": ""},
    )


def test_articles():
    s = _setup()
    form = {
        "name": "Fuzz Article",
        "article_type": "biere",
        "volume_cl": "25",
        "is_alcohol": "on",
        "alcohol_degree": "5",
        "price_std": "1",
        "price_team": "1",
    }
    _fuzz_form("/admin/articles/nouveau", form)
    _fuzz_form(f"/admin/articles/{s['article']}", form)


def test_tireuses_futs_evenements():
    s = _setup()
    keg = {
        "name": "Fut",
        "alcohol_degree": "5",
        "volume_l": "30",
        "brest_half_std": "1",
        "brest_pint_std": "2",
        "brest_pot_std": "3",
        "brest_half_team": "1",
        "brest_pint_team": "2",
        "brest_pot_team": "3",
    }
    _fuzz_form("/admin/tireuses/kegs/nouveau", keg)
    _fuzz_form(f"/admin/tireuses/kegs/{s['keg']}", keg)
    _fuzz_form("/admin/tireuses/taps/nouveau", {"name": "Tireuse fuzz"})
    _fuzz_form(f"/admin/tireuses/taps/{s['tap']}/renommer", {"name": "Tireuse renommee"})
    _fuzz_form(f"/admin/tireuses/taps/{s['tap']}/assigner", {"keg_id": str(s["keg"])})
    _fuzz_form(
        "/admin/evenements/nouveau",
        {"name": "Evt", "starts_at": "2026-12-01T20:00", "ends_at": "2026-12-02T02:00"},
    )
    event = f"/admin/evenements/{s['event']}"
    _fuzz_form(
        event,
        {
            "action": "edit",
            "name": "Evt",
            "starts_at": "2026-12-01T20:00",
            "ends_at": "2026-12-02T02:00",
        },
    )
    _fuzz_form(
        event,
        {
            "action": "add_article",
            "name": "Art evt",
            "price_std": "1",
            "price_team": "1",
            "volume_cl": "25",
        },
    )
    _fuzz_form(event, {"action": "del_article", "article_id": str(s["article"])})


def test_tireuse_nom_long_et_article_derive():
    """Nom de tireuse et de fût de 255 caractères : le nom d'article généré
    (« Pinte de <tireuse> (<fût>) ») dépasse 255 et ne doit pas faire échouer l'affectation."""
    s = _setup()
    client, csrf = s["client"], s["csrf"]
    res = client.post(
        f"/admin/tireuses/taps/{s['tap']}/renommer", data={"_csrf": csrf, "name": "T" * 255}
    )
    _expect(res.status_code < 500, f"renommage (255 car.) : {res.status_code}")
    with s["app"].app_context():
        keg = Keg(name="F" * 255, volume_l=30)
        db.session.add(keg)
        db.session.commit()
        keg_id = keg.id
    res = client.post(
        f"/admin/tireuses/taps/{s['tap']}/assigner", data={"_csrf": csrf, "keg_id": str(keg_id)}
    )
    _expect(res.status_code < 500, f"affectation (noms de 255 car.) : {res.status_code}")


def test_parametres_et_equipe():
    s = _setup()
    _fuzz_form(
        "/admin/module-dev",
        {"action": "add_link", "label": "Lien", "url": "https://exemple.fr", "position": "1"},
    )
    _fuzz_form("/admin/module-dev", {"action": "del_link", "link_id": "1"})
    _fuzz_form(
        "/admin/module-dev",
        {
            "action": "settings",
            "site_name": "Foy'z",
            "overdraft_limit": "5",
            "max_postits_public": "5",
            "homepage_text": "x",
            "link_hosting": "https://h.fr",
            "link_database": "https://d.fr",
            "link_repository": "https://r.fr",
        },
    )
    for path, form in (
        ("/equipe/rechargement", {"user_id": str(s["student"]), "amount": "5", "method": "cb"}),
        ("/equipe/retrait", {"user_id": str(s["student"]), "amount": "1"}),
        (
            "/equipe/transfert",
            {"from_id": str(s["student"]), "to_id": str(s["target"]), "amount": "1"},
        ),
        ("/equipe/consignes/retour", {"user_id": str(s["student"]), "count": "1"}),
        (
            "/equipe/notes/action",
            {"action": "create", "content": "n", "note_id": "1", "scope": "public"},
        ),
        ("/equipe/historique/annuler", {"transaction_ids": "1", "admin_password": "x"}),
    ):
        _fuzz_form(path, form)


def test_api_caisse():
    s = _setup()
    _fuzz_json(
        "/api/purchase",
        {
            "items": [{"article_id": s["article"], "quantity": 1}],
            "contributors": [s["student"]],
            "deposit_glasses": 0,
            "direct": False,
            "payment_method": "cb",
            "admin_password": "x",
            "idempotency_key": "fuzz-1",
        },
    )
    _fuzz_json("/api/glasses/return", {"user_id": s["student"], "count": 1})
    # Éléments imbriqués : champs d'une ligne de commande.
    for field in ("article_id", "quantity"):
        for value in JSON_BAD:
            line = {"article_id": s["article"], "quantity": 1, field: value}
            res = s["client"].post(
                "/api/purchase",
                json={"items": [line], "contributors": [s["student"]]},
                headers={"X-CSRFToken": s["csrf"]},
            )
            _expect(res.status_code < 500, f"ligne {field}={str(value)[:12]!r} : {res.status_code}")


def test_filtres_et_listes():
    _fuzz_get(
        "/admin/comptes",
        {
            "q": "a",
            "promo": "2027",
            "equipe": "mandat",
            "restriction": "oui",
            "tri": "nom",
            "page": "1",
        },
    )
    _fuzz_get(
        "/admin/articles",
        {"q": "a", "type": "biere", "volume": "25", "prix_min": "1", "prix_max": "9", "tri": "nom"},
    )
    _fuzz_get(
        "/admin/journal",
        {
            "tab": "actions",
            "action": "x",
            "user": "a",
            "from": "2026-01-01",
            "to": "2026-12-31",
            "page": "1",
        },
    )
    _fuzz_get(
        "/admin/journal", {"tab": "connexions", "user": "a", "from": "2026-01-01", "page": "1"}
    )
    _fuzz_get(
        "/equipe/historique",
        {
            "type": "achat",
            "campus": "brest",
            "user": "a",
            "from": "2026-01-01",
            "to": "2026-12-31",
            "page": "1",
        },
    )
    _fuzz_get("/api/students", {"q": "a"})
    _fuzz_get("/equipe/statistiques", {"from": "2026-01-01", "to": "2026-12-31", "q": "a"})
    _fuzz_get("/equipe/tresorerie", {"from": "2026-01-01", "to": "2026-12-31"})


def test_connexion():
    app = _setup()["app"]
    errors = []
    for value in BAD:
        client = app.test_client()
        html = client.get("/connexion").get_data(as_text=True)
        token = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
        res = client.post(
            "/connexion",
            data={"username": value, "password": value, "campus": "brest", "_csrf": token},
        )
        if res.status_code >= 500:
            errors.append(f"{value[:12]!r} -> {res.status_code}")
    _expect(not errors, f"connexion : {errors[:3]}")


def test_passerelle_et_identifiants_dans_l_url():
    s = _setup()
    client = s["client"]
    errors = []
    big = "9" * 25
    for path in (
        f"/admin/comptes/{big}",
        f"/admin/articles/{big}",
        f"/admin/equipe/{big}",
        f"/admin/evenements/{big}",
        f"/admin/tireuses/kegs/{big}",
        f"/api/wallet/{big}",
        f"/passerelle/{LONG}",
        "/passerelle/fuzz%00",
        "/passerelle/fuzz-passerelle",
        "/passerelle/fuzz-passerelle/catalogue",
        "/uploads/" + LONG,
        "/uploads/a%00b",
    ):
        try:
            status = client.get(path).status_code
        except Exception as exc:
            status = f"exception {type(exc).__name__}"
        if not isinstance(status, int) or status >= 500:
            errors.append(f"GET {path[:40]} -> {status}")
    for path in (
        f"/admin/comptes/{big}/supprimer",
        f"/admin/articles/{big}/supprimer",
        f"/admin/tireuses/taps/{big}/renommer",
    ):
        try:
            status = client.post(path, data={"_csrf": s["csrf"], "name": "x"}).status_code
        except Exception as exc:
            status = f"exception {type(exc).__name__}"
        if not isinstance(status, int) or status >= 500:
            errors.append(f"POST {path[:40]} -> {status}")
    _expect(not errors, f"identifiants/passerelle : {errors[:4]}")
    # Encaissement par la passerelle (lien public) : champs extrêmes.
    anonymous = s["app"].test_client()
    errors = []
    base = {
        "items": [{"article_id": s["article"], "quantity": 1}],
        "contributors": [s["student"]],
        "deposit_glasses": 0,
        "idempotency_key": "fuzz-gw",
    }
    for field in base:
        for value in JSON_BAD:
            try:
                status = anonymous.post(
                    "/passerelle/fuzz-passerelle/encaisser", json={**base, field: value}
                ).status_code
            except Exception as exc:
                status = f"exception {type(exc).__name__}"
            if not isinstance(status, int) or status >= 500:
                errors.append(f"{field}={str(value)[:12]!r} -> {status}")
    _expect(not errors, f"encaissement passerelle : {errors[:4]}")


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
