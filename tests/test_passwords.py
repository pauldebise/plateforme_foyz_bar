"""Tests de la politique de mot de passe (P10-5).

Exécutable sans pytest : python -m tests.test_passwords

Couvre :
- politique de mot de passe (longueur, diversité, mots courants, identifiant) ;
- application de la politique aux formulaires d'administration.
"""

import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_passwords_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import User  # noqa: E402
from app.services import passwords  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
MEMBER_PASSWORD = "Phrase-Membre-2026!"


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


def _seed_member(username, password=MEMBER_PASSWORD):
    app = create_app()
    with app.app_context():
        user = db.session.query(User).filter(User.username == username).first()
        if user is None:
            user = User(
                name=f"Membre {username}",
                username=username,
                team_status="mandat",
                team_campus="brest",
                password_hash=generate_password_hash(password),
            )
            db.session.add(user)
            db.session.commit()
        return user.id


def test_a_password_policy():
    _expect(passwords.validate("court") is not None, "longueur minimale")
    _expect(passwords.validate("motdepasse1234") is not None, "mot de passe courant refusé")
    _expect(passwords.validate("azertyuiopqs") is not None, "azerty refusé")
    _expect(passwords.validate("aAaA1111!!!!") is None, "phrase robuste acceptée")
    _expect(passwords.validate("Paul-Ensta-2026!") is None, "phrase robuste acceptée (2)")
    _expect(
        passwords.validate("Phrase-Paul-2026!", name="Paul Debise") is not None,
        "nom du compte refusé",
    )
    _expect(
        passwords.validate("Phrase-pdupont-2026!", username="pdupont") is not None,
        "identifiant refusé",
    )
    _expect(passwords.validate("abcdefghijklm") is not None, "pas assez de diversité")


def test_a2_politique_equipe():
    kwargs = {"min_length": passwords.TEAM_MIN_LENGTH, "require_diversity": False}
    _expect(passwords.validate("court", **kwargs) is not None, "équipe : longueur minimale")
    _expect(
        passwords.validate("motdepasse1234", **kwargs) is not None,
        "équipe : mot de passe courant refusé",
    )
    _expect(
        passwords.validate("Phrase-Paul-2026!", name="Paul Debise", **kwargs) is not None,
        "équipe : nom du compte refusé",
    )
    _expect(
        passwords.validate("huitlettres", **kwargs) is None,
        "équipe : 8 caractères sans diversité acceptés",
    )


def test_b_politique_dans_administration():
    app = create_app()
    with app.app_context():
        target = User(name="Cible Politique", username="cible.politique")
        db.session.add(target)
        db.session.commit()
        target_id = target.id
        before = target.password_hash

    client = app.test_client()
    _expect(_login(client).status_code == 302, "connexion administrateur")
    token = _csrf(client, "/admin/equipe")

    client.post(
        f"/admin/equipe/{target_id}",
        data={"team_status": "mandat", "password": "court", "_csrf": token},
    )
    with app.app_context():
        _expect(
            db.session.get(User, target_id).password_hash == before, "mot de passe faible refusé"
        )

    client.post(
        f"/admin/equipe/{target_id}",
        data={"team_status": "mandat", "password": "motdepasse1234", "_csrf": token},
    )
    with app.app_context():
        _expect(
            db.session.get(User, target_id).password_hash == before,
            "mot de passe courant refusé",
        )

    client.post(
        f"/admin/equipe/{target_id}",
        data={"team_status": "mandat", "password": "Phrase-Solide-2026!", "_csrf": token},
    )
    with app.app_context():
        stored = db.session.get(User, target_id).password_hash
        _expect(stored != before and check_password_hash(stored, "Phrase-Solide-2026!"), "accepté")

    # Politique équipe : 8 caractères suffisent, sans diversité imposée.
    client.post(
        f"/admin/equipe/{target_id}",
        data={"team_status": "mandat", "password": "huitlettres", "_csrf": token},
    )
    with app.app_context():
        stored = db.session.get(User, target_id).password_hash
        _expect(check_password_hash(stored, "huitlettres"), "équipe : accepté")

    # Mot de passe administrateur : min 8 caractères, sans diversité exigée.
    from app.services.settings import check_admin_password

    client.post(
        "/admin/module-dev",
        data={
            "action": "password",
            "current_password": ADMIN_PASSWORD,
            "new_password": "court",
            "_csrf": token,
        },
    )
    with app.app_context():
        _expect(
            check_admin_password(ADMIN_PASSWORD, "brest"), "mot de passe administrateur inchangé"
        )

    client.post(
        "/admin/module-dev",
        data={
            "action": "password",
            "current_password": ADMIN_PASSWORD,
            "new_password": "huitcarac",
            "_csrf": token,
        },
    )
    with app.app_context():
        _expect(check_admin_password("huitcarac", "brest"), "admin : 8 caractères sans diversité")


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
