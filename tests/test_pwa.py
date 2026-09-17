"""Tests PWA et mode hors ligne de la caisse (P10-6).

Exécutable sans pytest : python -m tests.test_pwa

Couvre :
- manifeste installable (nom, start_url, icônes 192/512, raccourcis) ;
- service worker servi à la racine (portée complète, jamais mis en cache)
  et liste de pré-cache entièrement résolvable ;
- page /hors-ligne ;
- accroches de la file d'attente (IndexedDB, clé d'idempotence, bannière,
  bouton Synchroniser) et CSP (worker-src, manifest-src) ;
- icônes PNG valides aux bonnes dimensions.
"""

import json
import os
import re
import struct
import sys
import tempfile
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

ADMIN_PASSWORD = "mot-de-passe-admin"
STATIC = ROOT / "app" / "static"


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


def test_service_worker():
    app = create_app()
    res = app.test_client().get("/sw.js")
    _expect(res.status_code == 200, f"service worker servi ({res.status_code})")
    _expect(
        res.headers.get("Service-Worker-Allowed") == "/",
        "portée racine autorisée",
    )
    _expect(res.headers.get("Cache-Control") == "no-cache", "service worker non mis en cache")
    body = res.get_data(as_text=True)
    _expect("javascript" in res.headers["Content-Type"], "type JavaScript")
    _expect("'install'" in body and "'fetch'" in body, "événements install/fetch")
    _expect("VERSION" in body and "/hors-ligne" in body, "pré-cache versionné")
    _expect("/equipe/historique" not in body, "aucune page sensible pré-cachée")


def test_precache_urls_resolvent():
    app = create_app()
    client = app.test_client()
    body = (STATIC / "js" / "sw.js").read_text(encoding="utf-8")
    precache = re.search(r"const PRECACHE = \[(.*?)\];", body, re.S)
    _expect(precache is not None, "liste PRECACHE présente")
    urls = re.findall(r"'([^']+)'", precache.group(1))
    _expect(len(urls) >= 10, f"liste de pré-cache fournie ({len(urls)})")
    for url in urls:
        res = client.get(url)
        _expect(res.status_code == 200, f"pré-cache résolvable : {url} ({res.status_code})")


def test_offline_page():
    app = create_app()
    res = app.test_client().get("/hors-ligne")
    _expect(res.status_code == 200, f"page hors ligne servie ({res.status_code})")
    page = res.get_data(as_text=True)
    _expect("Pas de connexion" in page, "message hors ligne")
    _expect("caisse" in page.lower(), "renvoi vers la caisse")


def test_icones_png():
    for size in (192, 512):
        data = (STATIC / "icons" / f"icon-{size}.png").read_bytes()
        _expect(data[:8] == b"\x89PNG\r\n\x1a\n", f"signature PNG {size}")
        width, height = struct.unpack(">II", data[16:24])
        _expect((width, height) == (size, size), f"dimensions {size}x{size}")


def test_accroches_hors_ligne_et_csp():
    offline_js = (STATIC / "js" / "offline.js").read_text(encoding="utf-8")
    for token in (
        "indexedDB",
        "foyz-offline",
        "queueSale",
        "X-CSRFToken",
        "navigator.onLine",
        "data-offline-flush",
        "register('/sw.js')",
    ):
        _expect(token in offline_js, f"offline.js : {token}")
    payment_js = (STATIC / "js" / "payment.js").read_text(encoding="utf-8")
    _expect("FoyzOffline.queueSale" in payment_js, "la caisse met en file les échecs réseau")
    _expect("resetCartState" in payment_js, "panier réinitialisé après mise en file")

    app = create_app()
    client = app.test_client()
    _expect(_login(client).status_code == 302, "connexion administrateur")
    res = client.get("/equipe/paiement")
    page = res.get_data(as_text=True)
    _expect("offline-banner" in page and "data-offline-flush" in page, "bannière hors ligne")
    _expect("js/offline.js" in page, "file d'attente chargée sur la caisse")
    _expect('rel="manifest"' in page, "manifeste lié")
    _expect('name="theme-color"' in page, "theme-color présent")
    csp = res.headers["Content-Security-Policy"]
    _expect("worker-src 'self'" in csp, "CSP worker-src")
    _expect("manifest-src 'self'" in csp, "CSP manifest-src")


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
