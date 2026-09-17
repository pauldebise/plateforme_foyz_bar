"""Tests de sécurité applicative (phase 4 du plan d'action).

Exécutable sans pytest : python -m tests.test_hardening

Couvre :
- T-4.1 : plus d'injection HTML/JS (host_url retiré, handlers inline absents,
  assets locaux), SVG refusé au téléversement ;
- T-4.2 : audit et purge des mots de passe hérités, conversion à la connexion ;
- T-4.3 : CSP stricte, Cache-Control no-store sur les pages authentifiées, HSTS
  quand HTTPS est déclaré, assets servis localement ;
- T-4.4 : cents() borné, longueurs tronquées, pas de suppression inter-campus,
  repli du mot de passe admin limité au développement.
"""

import io
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_hardening_"))
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP / 'app.db'}"
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from flask import g  # noqa: E402
from werkzeug.datastructures import FileStorage  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Article, Keg, User  # noqa: E402
from app.routes.admin import _deletion_campus_ok, save_upload  # noqa: E402
from app.services import legacy_passwords as LP  # noqa: E402
from app.services.settings import check_admin_password, set_setting  # noqa: E402
from app.utils import cents, clamp_text, safe_color  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(html):
    match = re.search(r'name="csrf-token" content="([^"]+)"', html)
    _expect(match is not None, "jeton CSRF présent dans la page")
    return match.group(1)


def _login(client, username, password, campus="brest"):
    html = client.get("/connexion").get_data(as_text=True)
    token = _csrf(html)
    return client.post(
        "/connexion",
        data={"username": username, "password": password, "campus": campus, "_csrf": token},
    )


def _client(app):
    client = app.test_client()
    res = _login(client, "admin", ADMIN_PASSWORD)
    _expect(res.status_code == 302, f"connexion admin réussie ({res.status_code})")
    return client


def test_cents_and_helpers_are_bounded():
    _expect(cents("12,50") == 1250, "cents accepte la virgule")
    _expect(cents(None) == 0, "cents(None) -> 0")
    for bad in ("inf", "-inf", "nan", "abc", "1e400", "9999999999"):
        try:
            cents(bad)
        except ValueError:
            continue
        raise AssertionError(f"cents({bad!r}) aurait dû lever")
    _expect(clamp_text("x" * 400, 255) == "x" * 255, "troncature à la longueur de colonne")
    _expect(clamp_text(None, 10) is None, "clamp_text(None)")
    _expect(safe_color("#a1b2c3") == "#a1b2c3", "couleur hexadécimale conservée")
    _expect(safe_color("red; } body { display:none") == "#804db3", "injection CSS neutralisée")


def test_legacy_password_audit_conversion_and_purge():
    app = create_app()
    with app.app_context():
        md5_user = User(
            name="Ancien Md5",
            username="ancien.md5",
            legacy_password="5f4dcc3b5aa765d61d8327deb882cf99",
        )
        team_user = User(
            name="Ancien Équipe",
            username="ancien.equipe",
            team_status="mandat",
            team_campus="brest",
            legacy_password="motdepasse",
        )
        db.session.add_all([md5_user, team_user])
        db.session.commit()

        _expect(LP.format_of(md5_user.legacy_password) == "md5", "format md5 détecté")
        _expect(LP.format_of(team_user.legacy_password) == "plaintext", "clair détecté")
        counts = LP.audit()
        _expect(counts.get("md5") == 1 and counts.get("plaintext") == 1,
                f"audit compte les formats ({dict(counts)})")

        # conversion à la connexion
        client = app.test_client()
        res = _login(client, "ancien.equipe", "motdepasse")
        _expect(res.status_code == 302, f"connexion legacy réussie ({res.status_code})")
        db.session.expire_all()
        converted = db.session.get(User, team_user.id)
        _expect(converted.legacy_password is None, "legacy vidé après conversion")
        _expect(converted.password_hash, "hash werkzeug posé après conversion")

        # purge : tous les comptes restants
        removed = LP.purge()
        _expect(removed == 1, f"un compte purgé ({removed})")
        db.session.expire_all()
        _expect(
            all(u.legacy_password is None for u in db.session.query(User).all()),
            "legacy_password nul partout après purge",
        )


def test_security_headers_and_offline_assets():
    app = create_app()
    client = app.test_client()

    page = client.get("/")
    _expect("Content-Security-Policy" in page.headers, "CSP présente")
    csp = page.headers["Content-Security-Policy"]
    _expect("script-src 'self'" in csp and "'unsafe-eval'" not in csp, "CSP script stricte")
    _expect("object-src 'none'" in csp, "object-src neutralisé")
    _expect("jsdelivr" not in page.get_data(as_text=True), "aucun asset CDN dans le HTML")

    for asset in (
        "/static/vendor/bootstrap/bootstrap.min.css",
        "/static/vendor/bootstrap/bootstrap.bundle.min.js",
        "/static/vendor/bootstrap-icons/bootstrap-icons.min.css",
        "/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2",
        "/static/vendor/chart/chart.umd.min.js",
    ):
        _expect(client.get(asset).status_code == 200, f"asset local servi : {asset}")

    # un fichier téléversé (même SVG résiduel) est servi sans aucun droit d'exécution
    upload = Path(os.environ["UPLOAD_DIR"]) / "residuel.svg"
    upload.write_text("<svg onload=alert(1)></svg>")
    res = client.get("/uploads/residuel.svg")
    _expect(res.status_code == 200, f"fichier téléversé servi ({res.status_code})")
    _expect(
        res.headers.get("Content-Security-Policy") == "default-src 'none'",
        "CSP restrictive sur /uploads",
    )

    # pages authentifiées : pas de cache navigateur
    authed = _client(app)
    res = authed.get("/equipe/paiement")
    _expect(res.status_code == 200, f"page équipe servie ({res.status_code})")
    _expect(res.headers.get("Cache-Control") == "no-store, private", "no-store en session équipe")
    _expect("Cache-Control" not in page.headers, "pas de no-store sur le public")

    # HSTS seulement quand l'application est servie en HTTPS
    app.config["SESSION_COOKIE_SECURE"] = True
    _expect("Strict-Transport-Security" in authed.get("/equipe/paiement").headers, "HSTS activé")
    app.config["SESSION_COOKIE_SECURE"] = False
    _expect("Strict-Transport-Security" not in authed.get("/equipe/paiement").headers,
            "pas de HSTS hors HTTPS")


def test_no_inline_handlers_and_no_host_url():
    app = create_app()
    authed = _client(app)
    html = authed.get("/admin/evenements").get_data(as_text=True)
    _expect(not re.search(r"\son[a-z]+=", html), "aucun handler inline dans le HTML")

    from app.models import Event
    from app.utils import utcnow

    with app.app_context():
        ev = Event(name="Événement lien", campus="brest", starts_at=utcnow(), ends_at=utcnow(), token="tok-test")
        db.session.add(ev)
        db.session.commit()
        event_id = ev.id
    page = authed.get(f"/admin/evenements/{event_id}").get_data(as_text=True)
    _expect('data-copy="/passerelle/tok-test"' in page, "lien copié via data-copy relatif")
    _expect("http://localhost" not in page and "request.host_url" not in page,
            "URL de passerelle non construite depuis l'en-tête Host")


def test_svg_upload_is_refused():
    app = create_app()
    with app.app_context(), app.test_request_context("/"):
        svg = FileStorage(stream=io.BytesIO(b"<svg onload=alert(1)></svg>"), filename="logo.svg")
        _expect(
            save_upload(svg, allowed=(".jpg", ".jpeg", ".png", ".webp")) is None,
            "SVG refusé comme logo",
        )
        png = FileStorage(stream=io.BytesIO(b"\x89PNG"), filename="logo.png")
        _expect(
            save_upload(png, allowed=(".jpg", ".jpeg", ".png", ".webp")),
            "PNG accepté",
        )


def test_admin_rejects_invalid_numbers_without_500():
    app = create_app()
    authed = _client(app)
    token = _csrf(authed.get("/admin/articles/nouveau").get_data(as_text=True))
    before = None
    with app.app_context():
        before = db.session.query(Article).count()

    res = authed.post(
        "/admin/articles/nouveau",
        data={
            "name": "Article piégé",
            "article_type": "biere",
            "price_std_brest": "inf",
            "price_team_brest": "0",
            "_csrf": token,
        },
    )
    _expect(res.status_code == 302, f"prix invalide -> redirection, pas 500 ({res.status_code})")
    with app.app_context():
        _expect(db.session.query(Article).count() == before, "aucun article créé avec un prix invalide")

    res = authed.post(
        "/admin/tireuses/kegs/nouveau",
        data={"name": "Fût piégé", "volume_l": "nan", "alcohol_degree": "5", "_csrf": token},
    )
    _expect(res.status_code == 302, f"volume invalide -> redirection ({res.status_code})")
    with app.app_context():
        _expect(
            db.session.query(Keg).filter(Keg.name == "Fût piégé").first() is None,
            "aucun fût créé avec un volume invalide",
        )


def test_long_names_are_truncated():
    app = create_app()
    authed = _client(app)
    token = _csrf(authed.get("/admin/articles/nouveau").get_data(as_text=True))
    long_name = "N" * 300
    res = authed.post(
        "/admin/articles/nouveau",
        data={
            "name": long_name,
            "article_type": "biere",
            "price_std_brest": "2,50",
            "price_team_brest": "2,00",
            "_csrf": token,
        },
    )
    _expect(res.status_code == 302, "article long accepté avec troncature")
    with app.app_context():
        stored = db.session.query(Article).filter(Article.name == "N" * 255).first()
        _expect(stored is not None, "nom stocké tronqué à 255 caractères")


def test_account_deletion_is_scoped_to_campus():
    app = create_app()
    with app.app_context(), app.test_request_context("/"):
        brest_mandat = User(name="Mandat Brest", username="mandat.test", team_status="mandat",
                            team_campus="brest")
        paris_member = User(name="Mandat Paris", username="mandat.paris", team_status="mandat",
                            team_campus="paris")
        paris_balance = User(name="Élève Paris", username="eleve.paris.debt")
        neutral = User(name="Élève Neutre", username="eleve.neutre")
        db.session.add_all([brest_mandat, paris_member, paris_balance, neutral])
        db.session.flush()
        paris_balance.wallet("paris").balance = 1200
        neutral.wallet("brest")
        neutral.wallet("paris")
        db.session.commit()

        g.current_user = brest_mandat
        _expect(not _deletion_campus_ok(paris_member), "membre de l'autre campus protégé")
        _expect(not _deletion_campus_ok(paris_balance), "solde sur l'autre campus protégé")
        _expect(_deletion_campus_ok(neutral), "compte sans engagement supprimable")


def test_admin_password_fallback_is_dev_only():
    app = create_app()
    with app.app_context():
        set_setting("admin_password_hash", "")
        db.session.commit()
        app.config["DEFAULT_ADMIN_PASSWORD"] = "admin"
        app.config["DEBUG"] = False
        _expect(not check_admin_password("admin"), "repli en clair refusé hors développement")
        app.config["DEBUG"] = True
        _expect(check_admin_password("admin"), "repli en clair toléré en développement")


def test_chart_pages_render_with_json_data():
    app = create_app()
    authed = _client(app)
    for path, marker in (("/equipe/statistiques", 'id="stats-data"'),
                         ("/equipe/tresorerie", 'id="treasury-data"')):
        res = authed.get(path)
        _expect(res.status_code == 200, f"{path} rendu ({res.status_code})")
        html = res.get_data(as_text=True)
        _expect(marker in html, f"données JSON présentes dans {path}")
        _expect("new Chart(" not in html, f"initialisation Chart hors HTML inline ({path})")


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
