"""Tests : noms seuls (sans surnom) dans l'historique et le journal d'actions.

Exécutable sans pytest : python -m tests.test_noms_seuls

Couvre : colonne Opérateur, libellé de l'opération et détail de l'historique ;
acteur et cible du journal d'actions (nom réel seul, sans surnom ni identifiant,
même si le surnom a changé depuis) ; libellé tronqué à la longueur de la
colonne ; libellé sans compte correspondant laissé tel quel ; les autres pages
(comptes) gardent le surnom.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_noms_seuls_"))
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
from app.models import Article, AuditLog, User  # noqa: E402
from app.services import transactions as T  # noqa: E402
from app.services.names import plain_names  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
OP_NICK = "SurnomOperateurTresLong"
STUDENT_NICK = "SurnomEtudiantTresLong"
CIBLE_NICK = "SurnomCibleTresLong"
STALE_NICK = "AncienSurnom"
LONG_NICK = "x" * 245
_client = None


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _user(name, nickname, team=False):
    user = User(
        name=name,
        nickname=nickname,
        username=name.lower().replace(" ", "."),
        password_hash=generate_password_hash("secret123"),
        team_status="mandat" if team else None,
        team_campus="brest" if team else None,
    )
    db.session.add(user)
    db.session.flush()
    user.wallet("brest").balance = 10_000
    return user


def _setup():
    global _client
    if _client is not None:
        return _client
    app = create_app()
    with app.app_context():
        operator = _user("Zn Operateur", OP_NICK, team=True)
        student = _user("Zn Etudiant", STUDENT_NICK)
        cible = _user("Zn Cible", CIBLE_NICK)
        sans_surnom = _user("Zn Identifiant", None)
        long_op = _user("Zn Long", LONG_NICK, team=True)
        article = Article(name="Zn Biere", article_type="biere", campus="brest", price_std=100)
        db.session.add(article)
        db.session.commit()
        for op in (operator, long_op):
            T.create_purchase(
                operator_label=op.display_name[:255],
                campus="brest",
                items=[{"article_id": article.id, "quantity": 1}],
                contributor_ids=[student.id],
            )
        db.session.add(
            AuditLog(
                actor=operator.display_name,
                target=student.display_name,
                action="compte.modification",
                campus="brest",
            )
        )
        # Cibles de compte avec surnom + identifiant, identifiant seul, et
        # surnom devenu obsolète depuis l'écriture de la ligne.
        db.session.add(
            AuditLog(
                actor=operator.display_name,
                target=f"{cible.display_name} ({cible.username})",
                action="compte.modification",
                campus="brest",
            )
        )
        db.session.add(
            AuditLog(
                actor=operator.display_name,
                target=f"{sans_surnom.name} ({sans_surnom.username})",
                action="compte.modification",
                campus="brest",
            )
        )
        db.session.add(
            AuditLog(
                actor=f"{operator.name} ({STALE_NICK})",
                target=f"{cible.name} ({STALE_NICK}) ({cible.username})",
                action="compte.modification",
                campus="brest",
            )
        )
        # Cible non liée à un compte : le préfixe ressemble à un nom de compte,
        # mais le libellé doit rester intact.
        db.session.add(
            AuditLog(
                actor=operator.display_name,
                target=f"{cible.name} (catégorie)",
                action="article.modification",
                campus="brest",
            )
        )
        db.session.add(AuditLog(actor="Inconnu (xyz)", target="Cible (abc)", action="x.test"))
        db.session.commit()
    client = app.test_client()
    html = client.get("/connexion").get_data(as_text=True)
    token = re.search(r'name="_csrf" value="([^"]+)"', html).group(1)
    res = client.post(
        "/connexion",
        data={"username": "admin", "password": ADMIN_PASSWORD, "campus": "brest", "_csrf": token},
    )
    _expect(res.status_code == 302, "connexion administrateur")
    _client = client
    return client


def test_historique_sans_surnom():
    page = _setup().get("/equipe/historique").get_data(as_text=True)
    _expect("Zn Operateur" in page, "nom de l'opérateur affiché")
    _expect("Zn Etudiant" in page, "nom de l'étudiant affiché (opération et détail)")
    _expect(OP_NICK not in page, "surnom de l'opérateur absent")
    _expect(STUDENT_NICK not in page, "surnom de l'étudiant absent")


def test_historique_libelle_tronque():
    """Un libellé tronqué à 255 caractères (surnom très long) donne aussi le nom seul."""
    page = _setup().get("/equipe/historique").get_data(as_text=True)
    _expect("Zn Long" in page, "nom de l'opérateur au surnom très long affiché")
    _expect("x" * 30 not in page, "fragment du surnom absent")


def test_journal_actions_sans_surnom():
    page = _setup().get("/admin/journal?tab=actions").get_data(as_text=True)
    _expect("Zn Operateur" in page and "Zn Etudiant" in page, "acteur et cible : noms affichés")
    _expect(OP_NICK not in page and STUDENT_NICK not in page, "surnoms absents du journal")
    _expect(
        "Inconnu (xyz)" in page and "Cible (abc)" in page, "libellé sans compte laissé tel quel"
    )


def test_journal_cible_noms_reels():
    """La colonne Cible ne montre que le nom réel, jamais surnom ni identifiant."""
    page = _setup().get("/admin/journal?tab=actions").get_data(as_text=True)
    for name in ("Zn Cible", "Zn Identifiant", "Zn Operateur"):
        _expect(name in page, f"{name} affiché dans le journal")
    for hidden in (CIBLE_NICK, "zn.cible", "zn.identifiant", STALE_NICK):
        _expect(hidden not in page, f"{hidden} absent de la colonne Cible")
    _expect("Cible (abc)" in page, "cible non liée à un compte laissée telle quelle")
    _expect(
        "Zn Cible (catégorie)" in page,
        "cible non liée à un compte dont le préfixe ressemble à un nom laissée telle quelle",
    )


def test_autres_pages_gardent_le_surnom():
    client = _setup()
    page = client.get("/admin/comptes?q=Zn+Operateur").get_data(as_text=True)
    _expect(OP_NICK in page, "onglet Comptes : surnom conservé")


def test_plain_names_service():
    app = create_app()
    with app.app_context():
        _setup()
        full = f"Zn Operateur ({OP_NICK})"
        found = plain_names({full, "Sans surnom", "Zn Operateur (autre surnom)", None, ""}, 255)
        _expect(found == {full: "Zn Operateur"}, f"seul le libellé exact est résolu ({found})")
        _expect(plain_names(set(), 255) == {}, "ensemble vide")


def test_plain_names_libelle_compte():
    app = create_app()
    with app.app_context():
        _setup()
        operator = db.session.query(User).filter(User.username == "zn.operateur").one()
        surname_username = f"{operator.name} ({operator.nickname}) ({operator.username})"
        username_only = f"{operator.name} ({operator.username})"
        stale_target = f"{operator.name} ({STALE_NICK}) ({operator.username})"
        stale_actor = f"{operator.name} ({STALE_NICK})"
        found = plain_names({surname_username, username_only}, 160)
        _expect(
            found == {surname_username: operator.name, username_only: operator.name},
            f"surnom et identifiant retirés ({found})",
        )
        found = plain_names({stale_target, stale_actor, "Sans compte (xyz)"}, 160, user_labels=True)
        _expect(found.get(stale_target) == operator.name, "surnom obsolète + identifiant retirés")
        _expect(found.get(stale_actor) == operator.name, "surnom obsolète retiré de l'acteur")
        _expect("Sans compte (xyz)" not in found, "libellé sans compte laissé tel quel")


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
