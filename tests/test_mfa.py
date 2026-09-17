"""Tests MFA TOTP et politique de mot de passe (P10-5).

Exécutable sans pytest : python -m tests.test_mfa

Couvre :
- TOTP conforme RFC 6238 (vecteurs SHA-1), fenêtre de tolérance, anti-rejeu ;
- codes de secours hachés, à usage unique ;
- politique de mot de passe (longueur, diversité, mots courants, identifiant) ;
- parcours complet : enrôlement, connexion en deux étapes, code invalide,
  code de secours, désactivation ;
- application de la politique aux formulaires d'administration.
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_mfa_"))
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
from app.services import passwords, totp  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
MEMBER_PASSWORD = "Phrase-Membre-2026!"

# RFC 6238, annexe B (SHA-1, 8 chiffres) : secret ASCII "12345678901234567890".
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
RFC_VECTORS = {
    59: "94287082",
    1111111109: "07081804",
    1111111111: "14050471",
    1234567890: "89005924",
    2000000000: "69279037",
}


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


def _enable_mfa(app, user_id, recovery_codes=None, last_counter=None):
    with app.app_context():
        user = db.session.get(User, user_id)
        user.totp_secret = totp.generate_secret()
        user.totp_enabled = True
        if recovery_codes:
            user.totp_recovery = totp.hash_recovery_codes(recovery_codes)
        user.totp_last_counter = last_counter
        db.session.commit()
        return user.totp_secret


def _set_last_counter(app, user_id, value):
    with app.app_context():
        db.session.get(User, user_id).totp_last_counter = value
        db.session.commit()


def test_a_rfc6238_vectors():
    for timestamp, expected in RFC_VECTORS.items():
        _expect(
            totp.code(RFC_SECRET, timestamp=timestamp, digits=8) == expected,
            f"vecteur RFC 6238 T={timestamp}",
        )


def test_b_verify_window_and_replay():
    secret = totp.generate_secret()
    moment = 1_700_000_000
    current = totp.code(secret, timestamp=moment)
    _expect(totp.verify(secret, current, timestamp=moment) is not None, "code courant accepté")
    _expect(
        totp.verify(secret, totp.code(secret, timestamp=moment - 30), timestamp=moment) is not None,
        "code du pas précédent accepté (fenêtre)",
    )
    _expect(
        totp.verify(secret, totp.code(secret, timestamp=moment - 120), timestamp=moment) is None,
        "code trop ancien refusé",
    )
    wrong = "000000" if current != "000000" else "000001"
    _expect(totp.verify(secret, wrong, timestamp=moment) is None, "code faux refusé")
    _expect(totp.verify(secret, "abc", timestamp=moment) is None, "saisie invalide refusée")
    counter = totp.verify(secret, current, timestamp=moment, last_counter=None)
    _expect(
        totp.verify(secret, current, timestamp=moment, last_counter=counter) is None,
        "rejeu du même compteur refusé",
    )
    uri = totp.provisioning_uri(secret, "paul.test")
    _expect(
        uri.startswith("otpauth://totp/") and "secret=" in uri and "digits=6" in uri,
        "URI otpauth complète",
    )


def test_c_recovery_codes():
    codes = totp.generate_recovery_codes()
    _expect(
        len(codes) == 8 and all(re.fullmatch(r"[A-Z2-9]{4}-[A-Z2-9]{4}", c) for c in codes),
        "8 codes",
    )
    stored = totp.hash_recovery_codes(codes)
    _expect("".join(codes[0].split("-")) not in stored, "codes jamais stockés en clair")
    remaining, ok = totp.consume_recovery_code(stored, codes[0])
    _expect(ok, "premier code valide")
    _expect(totp.remaining_recovery_codes(remaining) == 7, "code consommé retiré")
    _expect(not totp.consume_recovery_code(remaining, codes[0])[1], "code à usage unique")
    _expect(totp.consume_recovery_code(remaining, "AAAA-BBBB")[1] is False, "code inconnu refusé")
    _expect(totp.consume_recovery_code(None, codes[1])[1] is False, "absence de codes gérée")
    _expect(json.loads(remaining) and len(json.loads(remaining)) == 7, "stockage JSON valide")


def test_d_password_policy():
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


def test_e_enrolement_et_connexion():
    user_id = _seed_member("mfa.actif")
    app = create_app()
    client = app.test_client()
    _expect(_login(client, "mfa.actif", MEMBER_PASSWORD).status_code == 302, "connexion simple")

    token = _csrf(client, "/compte/securite")
    page = client.get("/compte/securite").get_data(as_text=True)
    _expect("Double authentification" in page, "page sécurité")
    _expect("désactivée" in page, "MFA désactivé au départ")

    res = client.post(
        "/compte/securite",
        data={"action": "start", "password": MEMBER_PASSWORD, "_csrf": token},
    )
    page = res.get_data(as_text=True)
    match = re.search(r'id="totp-secret"[^>]*>\s*([A-Z2-9]+)\s*<', page)
    _expect(match is not None, "secret affiché après confirmation du mot de passe")
    secret = match.group(1)
    with app.app_context():
        _expect(db.session.get(User, user_id).totp_enabled is False, "activation en attente")

    res = client.post(
        "/compte/securite",
        data={"action": "confirm", "code": totp.code(secret), "_csrf": token},
    )
    page = res.get_data(as_text=True)
    _expect("MFA activé" in page, "activation confirmée")
    codes = re.findall(r'class="user-select-all fs-6">([A-Z2-9]{4}-[A-Z2-9]{4})<', page)
    _expect(len(codes) == 8, f"codes de secours affichés une fois ({len(codes)})")
    with app.app_context():
        user = db.session.get(User, user_id)
        _expect(user.totp_enabled, "MFA actif en base")
        _expect(user.totp_last_counter is not None, "compteur anti-rejeu enregistré")

    client.post("/deconnexion", data={"_csrf": token})
    res = _login(client, "mfa.actif", MEMBER_PASSWORD)
    _expect(
        res.status_code == 302 and "/connexion/verification" in res.headers["Location"],
        "second facteur demandé",
    )

    page = client.get("/connexion/verification").get_data(as_text=True)
    _expect("Vérification" in page and "Code de secours" in page, "page de vérification")

    wrong = "000000" if totp.code(secret) != "000000" else "000001"
    res = client.post(
        "/connexion/verification",
        data={"code": wrong, "_csrf": _csrf(client, "/connexion/verification")},
    )
    _expect(res.status_code == 401, "mauvais code refusé")

    # On simule le passage de la fenêtre de 30 s (compteur anti-rejeu remis à zéro).
    _set_last_counter(app, user_id, None)
    res = client.post(
        "/connexion/verification",
        data={"code": totp.code(secret), "_csrf": _csrf(client, "/connexion/verification")},
    )
    _expect(res.status_code == 302, "bon code accepté")
    _expect(client.get("/equipe/paiement").status_code == 200, "session ouverte après MFA")


def test_f_code_de_secours():
    app = create_app()
    user_id = _seed_member("mfa.secours")
    codes = totp.generate_recovery_codes()
    _enable_mfa(app, user_id, recovery_codes=codes)

    client = app.test_client()
    _expect(
        _login(client, "mfa.secours", MEMBER_PASSWORD).status_code == 302, "mot de passe valide"
    )
    token = _csrf(client, "/connexion/verification")
    res = client.post(
        "/connexion/verification",
        data={"recovery_code": codes[0], "_csrf": token},
    )
    _expect(res.status_code == 302, "connexion par code de secours")
    with app.app_context():
        remaining = totp.remaining_recovery_codes(db.session.get(User, user_id).totp_recovery)
        _expect(remaining == 7, f"code consommé ({remaining})")

    client.post("/deconnexion", data={"_csrf": token})
    _expect(_login(client, "mfa.secours", MEMBER_PASSWORD).status_code == 302, "reconnexion")
    token = _csrf(client, "/connexion/verification")
    res = client.post(
        "/connexion/verification",
        data={"recovery_code": codes[0], "_csrf": token},
    )
    _expect(res.status_code == 401, "code de secours déjà utilisé refusé")
    res = client.post(
        "/connexion/verification",
        data={"recovery_code": "AAAA-BBBB", "_csrf": token},
    )
    _expect(res.status_code == 401, "code de secours inconnu refusé")


def test_g_desactivation():
    app = create_app()
    user_id = _seed_member("mfa.desactive")
    secret = _enable_mfa(app, user_id, recovery_codes=totp.generate_recovery_codes())

    client = app.test_client()
    _expect(_login(client, "mfa.desactive", MEMBER_PASSWORD).status_code == 302, "mot de passe")
    token = _csrf(client, "/connexion/verification")
    res = client.post(
        "/connexion/verification",
        data={"code": totp.code(secret), "_csrf": token},
    )
    _expect(res.status_code == 302, "connexion MFA")

    token = _csrf(client, "/compte/securite")
    _set_last_counter(app, user_id, None)
    res = client.post(
        "/compte/securite",
        data={
            "action": "disable",
            "password": MEMBER_PASSWORD,
            "code": totp.code(secret),
            "_csrf": token,
        },
    )
    _expect("MFA désactivé" in res.get_data(as_text=True), "désactivation confirmée")
    with app.app_context():
        user = db.session.get(User, user_id)
        _expect(not user.totp_enabled and user.totp_secret is None, "MFA retiré en base")

    client.post("/deconnexion", data={"_csrf": token})
    res = _login(client, "mfa.desactive", MEMBER_PASSWORD)
    _expect(
        res.status_code == 302 and "/connexion/verification" not in res.headers["Location"],
        "connexion simple après désactivation",
    )


def test_h_politique_dans_administration():
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

    # Mot de passe administrateur : min 12 caractères exigés, actuel inchangé sinon.
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
        _expect(check_admin_password(ADMIN_PASSWORD), "mot de passe administrateur inchangé")


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
