"""Tests PWA (manifeste installable) et retrait de l'encaissement hors ligne.

Exécutable sans pytest : python -m tests.test_pwa

Couvre :
- manifeste installable (nom, start_url, icônes 192/512, raccourcis) ;
- icônes PNG valides aux bonnes dimensions ;
- retrait complet du mode hors ligne : plus de file IndexedDB, plus de page
  /hors-ligne, la caisse avertit en cas de panne réseau et conserve le panier ;
- /sw.js réduit à une pierre tombale : les anciens postes désenregistrent la
  coquille hors ligne et purgent ses caches, sans plus rien intercepter.
"""

import json
import os
import re
import struct
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_pwa_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Event  # noqa: E402
from app.utils import utcnow  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
STATIC = ROOT / "app" / "static"
TEMPLATES = ROOT / "app" / "templates"


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(client, path="/connexion"):
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    _expect(match is not None, f"jeton CSRF présent sur {path}")
    return match.group(1)


def _login(client):
    token = _csrf(client)
    return client.post(
        "/connexion",
        data={"username": "admin", "password": ADMIN_PASSWORD, "campus": "brest", "_csrf": token},
    )


def test_manifest_installable():
    app = create_app()
    res = app.test_client().get("/manifest.webmanifest")
    _expect(res.status_code == 200, f"manifeste servi ({res.status_code})")
    _expect(
        res.headers["Content-Type"].startswith("application/manifest+json"),
        "type MIME du manifeste",
    )
    payload = json.loads(res.get_data(as_text=True))
    _expect(payload["name"], "nom renseigné")
    _expect(payload["start_url"] == "/" and payload["scope"] == "/", "portée racine")
    _expect(payload["display"] == "standalone", "affichage autonome")
    _expect(re.fullmatch(r"#[0-9a-fA-F]{6}", payload["theme_color"]), "theme_color hexadécimal")
    sizes = {icon["sizes"] for icon in payload["icons"]}
    _expect({"192x192", "512x512"} <= sizes, f"icônes 192 et 512 ({sizes})")
    for icon in payload["icons"]:
        _expect(icon["src"].startswith("/static/"), "icône servie localement")
    shortcuts = {item["name"]: item["url"] for item in payload["shortcuts"]}
    _expect(shortcuts.get("Caisse") == "/equipe/paiement", "raccourci caisse")


def test_icones_png():
    for size in (192, 512):
        data = (STATIC / "icons" / f"icon-{size}.png").read_bytes()
        _expect(data[:8] == b"\x89PNG\r\n\x1a\n", f"signature PNG {size}")
        width, height = struct.unpack(">II", data[16:24])
        _expect((width, height) == (size, size), f"dimensions {size}x{size}")


def test_service_worker_pierre_tombale():
    app = create_app()
    res = app.test_client().get("/sw.js")
    _expect(res.status_code == 200, f"pierre tombale servie ({res.status_code})")
    _expect(
        res.headers.get("Service-Worker-Allowed") == "/",
        "portée racine autorisée",
    )
    _expect(res.headers.get("Cache-Control") == "no-cache", "service worker non mis en cache")
    body = res.get_data(as_text=True)
    _expect("javascript" in res.headers["Content-Type"], "type JavaScript")
    for token in ("skipWaiting", "caches.delete", "unregister"):
        _expect(token in body, f"sw.js : {token}")
    _expect("respondWith" not in body, "plus aucune interception réseau")
    _expect("caches.open" not in body, "plus aucune mise en cache")


def test_encaissement_hors_ligne_retire():
    _expect(not (STATIC / "js" / "offline.js").exists(), "offline.js supprimé")
    _expect(
        not (TEMPLATES / "public" / "hors_ligne.html").exists(),
        "page hors ligne supprimée",
    )
    app = create_app()
    client = app.test_client()
    _expect(client.get("/hors-ligne").status_code == 404, "route /hors-ligne retirée")

    payment_js = (STATIC / "js" / "payment.js").read_text(encoding="utf-8")
    _expect("FoyzOffline" not in payment_js, "plus de file locale dans la caisse")
    _expect("queueSale" not in payment_js, "plus de mise en file des ventes")
    _expect("instanceof TypeError" in payment_js, "panne réseau traitée à part")

    _expect(_login(client).status_code == 302, "connexion administrateur")
    caisse = client.get("/equipe/paiement")
    page = caisse.get_data(as_text=True)
    _expect("offline-banner" not in page, "bannière hors ligne retirée de la caisse")
    _expect("data-offline-flush" not in page, "bouton Synchroniser retiré")
    _expect("js/offline.js" not in page, "offline.js plus chargé")
    _expect('rel="manifest"' in page, "manifeste toujours lié")
    csp = caisse.headers["Content-Security-Policy"]
    _expect("manifest-src 'self'" in csp, "CSP manifest-src")
    _expect("worker-src" not in csp, "CSP worker-src retirée (plus de service worker enregistré)")

    with app.app_context():
        ev = Event(
            name="Événement PWA",
            campus="brest",
            starts_at=utcnow(),
            ends_at=utcnow() + timedelta(hours=3),
            token="tok-pwa-test",
        )
        db.session.add(ev)
        db.session.commit()
    gw = client.get("/passerelle/tok-pwa-test")
    _expect(gw.status_code == 200, f"passerelle servie ({gw.status_code})")
    gw_page = gw.get_data(as_text=True)
    _expect("offline-banner" not in gw_page, "bannière hors ligne retirée de la passerelle")
    _expect("js/offline.js" not in gw_page, "offline.js plus chargé sur la passerelle")


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
