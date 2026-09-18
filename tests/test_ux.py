"""Tests UX, accessibilité et conformité (phase 8).

Exécutable sans pytest : python -m tests.test_ux

Couvre :
- T-8.1 : recalcul caisse, barre d'action mobile, toasts (garde-fous) ;
- T-8.2 : listes de résultats ARIA, focus visible, cibles tactiles ;
- T-8.3 : notes conservées (R16), annulation multiple de transactions (U8).
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_ux_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Note, Transaction, User  # noqa: E402
from app.services import transactions as T  # noqa: E402
from app.services.settings import set_setting  # noqa: E402

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


def _login(client, username="admin", password=ADMIN_PASSWORD):
    token = _csrf(client)
    res = client.post(
        "/connexion",
        data={"username": username, "password": password, "campus": "brest", "_csrf": token},
    )
    _expect(res.status_code == 302, f"connexion réussie ({res.status_code})")
    return _csrf(client)


def test_a_ux_garde_fous():
    app = create_app()
    client = app.test_client()

    # Les toasts remplacent les alert() : conteneur présent sur toutes les pages.
    home = client.get("/").get_data(as_text=True)
    _expect('id="toast-container"' in home, "conteneur de toasts présent")

    _login(client)
    payment = client.get("/equipe/paiement").get_data(as_text=True)
    _expect('id="mobile-pay-bar"' in payment, "barre d'action mobile sur la caisse")
    _expect(
        "data-pay" in payment and "data-total" in payment,
        "total et bouton pilotés par data-attributes",
    )

    payment_js = (STATIC / "js" / "payment.js").read_text(encoding="utf-8")
    _expect("alert(" not in payment_js, "plus d'alert() dans la caisse")
    _expect("location.reload" not in payment_js, "plus de rechargement automatique")
    _expect(
        "renderCatalog();" in payment_js and "renderCart();" in payment_js,
        "catalogue et panier recalculés",
    )

    app_js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    for token in ("listbox", "combobox", "aria-activedescendant", "'option'"):
        _expect(token in app_js, f"attribut d'accessibilité {token}")

    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    _expect(":focus-visible" in css, "focus visible")
    _expect("pointer: coarse" in css and "min-height: 44px" in css, "cibles tactiles de 44 px")


def test_b_notes_conservees():
    app = create_app()
    with app.app_context():
        set_setting("max_postits_private", "2")
        set_setting("max_postits_public", "1")
        for i in range(5):
            db.session.add(Note(content=f"Note privée {i}", is_public=False, author_name="Test"))
        for i in range(3):
            db.session.add(Note(content=f"Note publique {i}", is_public=True, author_name="Test"))
        db.session.commit()
        db.session.expire_all()

    client = app.test_client()
    token = _login(client)
    page = client.get("/equipe/notes").get_data(as_text=True)
    _expect("Note privée 4" in page and "Note privée 3" in page, "les plus récentes affichées")
    _expect("Note privée 0" not in page, "au-delà de la limite : masquée mais conservée")
    _expect("Afficher les 3 note(s) plus ancienne(s)" in page, "lien vers les anciennes")
    _expect("Note publique 2" in page and "Note publique 1" not in page, "limite publique")

    all_page = client.get("/equipe/notes?all=1").get_data(as_text=True)
    _expect(
        "Note privée 0" in all_page and "Note publique 0" in all_page,
        "affichage complet à la demande",
    )
    with app.app_context():
        _expect(
            db.session.query(Note).filter_by(is_public=False).count() == 5,
            "aucune note supprimée par la limite d'affichage",
        )

    # Publication puis suppression explicite : les notes restent, pas de purge auto.
    client.post(
        "/equipe/notes/action",
        data={
            "action": "create",
            "scope": "private",
            "content": "Note privée 5",
            "_csrf": token,
        },
    )
    with app.app_context():
        _expect(
            db.session.query(Note).filter_by(is_public=False).count() == 6,
            "publication sans purge des anciennes",
        )
        note_id = db.session.query(Note).filter_by(content="Note privée 5").one().id
    client.post(
        "/equipe/notes/action",
        data={
            "action": "delete",
            "note_id": str(note_id),
            "_csrf": token,
        },
    )
    with app.app_context():
        _expect(
            db.session.query(Note).filter_by(is_public=False).count() == 5,
            "suppression explicite toujours possible",
        )


def test_c_annulation_multiple():
    app = create_app()
    with app.app_context():
        membre = User(name="Delta Test", username="ux.delta")
        autre = User(name="Epsilon Paris", username="ux.epsilon")
        db.session.add_all([membre, autre])
        db.session.commit()
        ids = []
        for _ in range(3):
            t = T.create_reload(
                operator_label="test",
                campus="brest",
                user=membre,
                amount_cents=1000,
                payment_method="cb",
            )
            ids.append(t.id)
        paris = T.create_reload(
            operator_label="test", campus="paris", user=autre, amount_cents=500, payment_method="cb"
        )
        paris_id = paris.id

    client = app.test_client()
    token = _login(client)
    annuler = "/equipe/historique/annuler"

    # Mauvais mot de passe : rien n'est annulé.
    client.post(
        annuler,
        data={
            "transaction_ids": [str(i) for i in ids],
            "admin_password": "mauvais",
            "_csrf": token,
        },
    )
    with app.app_context():
        _expect(
            all(not db.session.get(Transaction, i).cancelled for i in ids),
            "mot de passe incorrect : aucune annulation",
        )

    # Sélection multiple (2 transactions) : soldes mis à jour.
    client.post(
        annuler,
        data={
            "transaction_ids": [str(ids[0]), str(ids[1])],
            "admin_password": ADMIN_PASSWORD,
            "_csrf": token,
        },
    )
    with app.app_context():
        reste = [db.session.get(Transaction, i).cancelled for i in ids]
        _expect(reste == [True, True, False], f"annulations multiples ({reste})")
        membre = db.session.query(User).filter_by(username="ux.delta").one()
        w = next(w for w in membre.wallets if w.campus == "brest")
        _expect(w.balance == 1000, f"solde recrédité ({w.balance})")

    # Transaction d'un autre campus refusée, celle du campus traitée.
    client.post(
        annuler,
        data={
            "transaction_ids": [str(ids[2]), str(paris_id)],
            "admin_password": ADMIN_PASSWORD,
            "_csrf": token,
        },
    )
    with app.app_context():
        _expect(db.session.get(Transaction, ids[2]).cancelled, "dernière annulée")
        _expect(
            not db.session.get(Transaction, paris_id).cancelled,
            "transaction de l'autre campus refusée",
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
