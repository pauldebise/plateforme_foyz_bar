"""Recherches de la caisse : insensibles aux majuscules ET aux accents.

« lea » doit retrouver « Léa », « biere » « Bière » : taper les accents ralentit
les encaissements. Couvre la normalisation (fold_text), la recherche d'étudiant
(serveur, route /api/students) et le filtre d'articles (navigateur, exécuté
avec node s'il est installé).

Exécutable sans pytest : python -m tests.test_recherche_accents
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_accents_"))
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
from app.models import User  # noqa: E402
from app.services import transactions as T  # noqa: E402
from app.utils import fold_text  # noqa: E402

PAYMENT_JS = ROOT / "app" / "static" / "js" / "payment.js"
_state = {}


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _setup():
    if _state:
        return _state
    app = create_app()
    people = {
        "lea_accent": dict(name="Léa Dupont", username="ident.xyz"),
        "lea_sans": dict(name="Lea Sansaccent", username="compte.a2"),
        "eloise": dict(name="Éloïse Martin", username="compte.a3", nickname="La Reine"),
        "zoe": dict(name="Zoé Cœur", username="compte.a4"),
        "muller": dict(name="François Müller", username="compte.a5", nickname="Ñoño"),
        "chevauche": dict(name="Anna", username="compte.a6", nickname="Bob"),
    }
    ids = {}
    with app.app_context():
        for key, data in people.items():
            user = User(promotion=2027, password_hash=generate_password_hash("x"), **data)
            db.session.add(user)
            db.session.flush()
            ids[key] = user.id
        db.session.commit()
    client = app.test_client()
    html = client.get("/connexion").get_data(as_text=True)
    token = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    res = client.post(
        "/connexion",
        data={
            "username": "admin",
            "password": "mot-de-passe-admin",
            "campus": "brest",
            "_csrf": token,
        },
    )
    _expect(res.status_code == 302, "connexion administrateur")
    _state.update(app=app, client=client, ids=ids)
    return _state


def _found(query):
    s = _setup()
    with s["app"].app_context():
        return {row["id"] for row in T.search_students(query, campus="brest")}


def _ids(*keys):
    s = _setup()
    return {s["ids"][k] for k in keys}


def test_fold_text():
    _expect(fold_text("Léa") == fold_text("LÉA") == fold_text("lea") == "lea", "Léa / LÉA / lea")
    _expect(fold_text("Éloïse") == "eloise", "tréma et accent aigu")
    _expect(fold_text("Cœur") == "coeur", "ligature œ")
    _expect(fold_text("Müller Ñoño") == "muller nono", "tréma et tilde")
    _expect(fold_text("Straße") == "strasse", "eszett")
    _expect(fold_text(None) == "" and fold_text("") == "", "valeurs vides")


def test_etudiant_sans_taper_les_accents():
    _expect(_found("lea") >= _ids("lea_accent", "lea_sans"), "« lea » trouve « Léa » et « Lea »")
    _expect(_found("léa") >= _ids("lea_accent", "lea_sans"), "« léa » trouve aussi « Lea »")
    _expect(_found("LÉA") >= _ids("lea_accent", "lea_sans"), "majuscule accentuée")
    _expect(_found("LEA") >= _ids("lea_accent", "lea_sans"), "majuscules sans accent")
    _expect(_ids("eloise") <= _found("eloise"), "« eloise » trouve « Éloïse »")
    _expect(_ids("eloise") <= _found("ELOISE"), "« ELOISE » trouve « Éloïse »")
    _expect(_ids("muller") <= _found("muller"), "« muller » trouve « Müller »")
    _expect(_ids("zoe") <= _found("zoe coeur"), "« zoe coeur » trouve « Zoé Cœur »")
    _expect(_ids("zoe") <= _found("zoé cœur"), "la saisie avec accents marche toujours")


def test_etudiant_mots_surnom_identifiant():
    _expect(_found("martin eloise") == _ids("eloise"), "mots dans le désordre")
    _expect(_found("eloise martin") == _ids("eloise"), "mots dans l'ordre")
    _expect(_found("reine") == _ids("eloise"), "recherche dans le surnom")
    _expect(_found("nono") == _ids("muller"), "surnom « Ñoño » trouvé par « nono »")
    _expect(_found("ident.xyz") == _ids("lea_accent"), "recherche dans l'identifiant")
    _expect(_found("IDENT") == _ids("lea_accent"), "identifiant : majuscules indifférentes")
    _expect(_found("dupont lea") == _ids("lea_accent"), "nom + prénom")
    _expect(_found("lea ident") == _ids("lea_accent"), "un mot dans le nom, un dans l'identifiant")
    _expect(_found("lea dupont") == _ids("lea_accent"), "« lea dupont » écarte « Lea Sansaccent »")


def test_etudiant_aucun_faux_positif():
    _expect(_found("none") == set(), "un surnom vide ne vaut pas « None »")
    _expect(_found("annabob") == set(), "un mot ne chevauche pas nom et surnom")
    _expect(_found("introuvable") == set(), "aucun résultat")
    _expect(_found("%") == set() and _found("_") == set(), "% et _ ne sont pas des jokers")
    _expect(len(_found("")) >= 6, "sans saisie : liste complète (bornée)")
    _expect(_found("   ") == _found(""), "espaces seuls = sans saisie")


def test_renommage_pris_en_compte():
    s = _setup()
    _expect(_found("hélène") == set() and _found("zoe coeur") == _ids("zoe"), "état initial")
    with s["app"].app_context():
        user = db.session.get(User, s["ids"]["zoe"])
        user.name = "Hélène Cœur"
        db.session.commit()
    _expect(_found("helene") == _ids("zoe"), "nouveau nom trouvé sans accent")
    _expect(_found("zoe") == set(), "ancien nom introuvable")
    with s["app"].app_context():
        user = db.session.get(User, s["ids"]["zoe"])
        user.name = "Zoé Cœur"
        db.session.commit()
    _expect(_found("zoe coeur") == _ids("zoe"), "nom rétabli")


def test_route_api_students():
    s = _setup()
    res = s["client"].get("/api/students?q=eloise&campus=brest")
    _expect(res.status_code == 200, f"/api/students : {res.status_code}")
    data = res.get_json()
    _expect([r["id"] for r in data] == [s["ids"]["eloise"]], "route : « eloise » trouve « Éloïse »")
    _expect("Éloïse Martin" in data[0]["name"], "le nom affiché garde ses accents")
    res = s["client"].get("/api/students?q=LEA&campus=brest")
    _expect({r["id"] for r in res.get_json()} >= _ids("lea_accent", "lea_sans"), "route : « LEA »")


def test_filtre_articles_navigateur():
    js = PAYMENT_JS.read_text(encoding="utf-8")
    _expect("fold(a.name).includes(q)" in js, "le filtre compare des textes normalisés")
    _expect("a.name.toLowerCase().includes" not in js, "plus de comparaison sensible aux accents")
    node = shutil.which("node")
    if not node:
        print("  (node absent : exécution de fold() ignorée)")
        return
    start = js.index("const FOLD_EXTRA")
    end = js.index("const $ =")
    script = js[start:end] + (
        "const cas = [['Bière','biere'],['BIÈRE','biere'],['Crêpe','crepe'],['Cœur','coeur'],"
        "['Müller','muller'],['', ''],[null, '']];"
        "for (const [a, b] of cas) { if (fold(a) !== b) { console.log('KO ' + a); process.exit(1); } }"
        "const noms = ['Bière blonde', 'Crêpe sucre', 'Chips'];"
        "const q = fold('BIERE');"
        "if (noms.filter((n) => fold(n).includes(q)).join() !== 'Bière blonde') process.exit(2);"
        "console.log('ok');"
    )
    res = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    _expect(
        res.returncode == 0 and "ok" in res.stdout, f"fold() dans node : {res.stdout}{res.stderr}"
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
