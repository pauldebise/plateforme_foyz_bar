"""Tests du limiteur de connexion partagé (B8) et des journaux/réglages (B7).

Exécutable sans pytest : python -m tests.test_connexion_journaux

Couvre :
- B8 : 10 tentatives libres par identifiant puis délai croissant, un mot de
  passe correct remet le compteur à zéro, compteur partagé en base, IPv6
  agrégée au /64, saisie brute jamais journalisée ;
- B7 : les durées des journaux filtrent l'affichage sans rien supprimer,
  réglages bornés (déconnexion >= 5 min), valeur invalide jamais enregistrée.

Compatible SQLite et PostgreSQL (FOYZ_TEST_DATABASE_URL).
"""

import itertools
import os
import re
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_connexion_"))
os.environ["DATABASE_URL"] = (
    os.environ.get("FOYZ_TEST_DATABASE_URL") or f"sqlite:///{_TMP / 'app.db'}"
)
os.environ["UPLOAD_DIR"] = str(_TMP / "uploads")
os.environ["SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["ADMIN_PASSWORD"] = "mot-de-passe-admin"
os.environ.pop("FLASK_ENV", None)

from sqlalchemy import select  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

import app.routes.auth as auth_module  # noqa: E402
from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import AuditLog, LoginLog, LoginThrottle, User  # noqa: E402
from app.services import loginthrottle as LT  # noqa: E402
from app.services import settings as S  # noqa: E402
from app.utils import utcnow  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
GOOD = "secret123"
_seq = itertools.count(1)


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(html):
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    _expect(match is not None, "jeton CSRF présent")
    return match.group(1)


def _post_login(client, username, password, ip="203.0.113.5"):
    token = _csrf(client.get("/connexion").get_data(as_text=True))
    return client.post(
        "/connexion",
        data={"username": username, "password": password, "campus": "brest", "_csrf": token},
        environ_base={"REMOTE_ADDR": ip},
    )


def _member(app):
    n = next(_seq)
    with app.app_context():
        user = User(
            name=f"Limiteur Test {n}",
            username=f"limiteur.test.{n}",
            password_hash=generate_password_hash(GOOD),
            team_status="mandat",
            team_campus="brest",
        )
        db.session.add(user)
        db.session.commit()
        return user.username


def _fresh():
    app = create_app()
    auth_module.reset_throttle(app)
    return app


def _age_throttle(app, seconds):
    with app.app_context():
        for row in db.session.scalars(select(LoginThrottle)).all():
            row.last_failure_at = row.last_failure_at - timedelta(seconds=seconds)
        db.session.commit()


# --------------------------------------------------------------------------- B8


def test_b8_delais_croissants():
    free = LT.ACCOUNT_FREE_ATTEMPTS
    _expect(LT.delay_seconds(free - 1, free) == 0, "sous le seuil : aucun délai")
    _expect(LT.delay_seconds(free, free) == 5, "10 échecs : 5 s")
    _expect(LT.delay_seconds(free + 1, free) == 10, "11 échecs : 10 s")
    _expect(LT.delay_seconds(free + 2, free) == 20, "12 échecs : 20 s")
    _expect(LT.delay_seconds(free + 500, free) == LT.MAX_DELAY_SECONDS, "délai plafonné")


def test_b8_dix_tentatives_libres_puis_delai():
    app = _fresh()
    username = _member(app)
    client = app.test_client()
    for i in range(LT.ACCOUNT_FREE_ATTEMPTS):
        res = _post_login(client, username, "mauvais")
        _expect(res.status_code == 401, f"échec {i + 1} : 401 ({res.status_code})")
    res = _post_login(client, username, GOOD)
    _expect(res.status_code == 429, f"11e tentative différée même correcte ({res.status_code})")
    _expect("Réessayez dans" in res.get_data(as_text=True), "message de délai")
    other = _member(app)
    _expect(_post_login(app.test_client(), other, GOOD).status_code == 302, "autre compte intact")

    _age_throttle(app, 10)
    res = _post_login(client, username, GOOD)
    _expect(res.status_code == 302, f"délai écoulé : connexion possible ({res.status_code})")
    with app.app_context():
        key = LT.account_key(username)
        _expect(db.session.get(LoginThrottle, key) is None, "succès : compteur remis à zéro")


def test_b8_delai_croit_a_chaque_echec_apres_le_seuil():
    app = _fresh()
    username = _member(app)
    client = app.test_client()
    for _ in range(LT.ACCOUNT_FREE_ATTEMPTS):
        _post_login(client, username, "mauvais")
    _age_throttle(app, 6)
    res = _post_login(client, username, "mauvais")
    _expect(res.status_code == 401, "après le délai, l'essai est évalué")
    with app.app_context():
        row = db.session.get(LoginThrottle, LT.account_key(username))
        _expect(row.failures == LT.ACCOUNT_FREE_ATTEMPTS + 1, "11 échecs comptés")
        _expect(LT.remaining_seconds(LT.account_key(username)) > 5, "délai porté à 10 s")


def test_b8_succes_remet_le_compteur_a_zero():
    app = _fresh()
    username = _member(app)
    client = app.test_client()
    for _ in range(LT.ACCOUNT_FREE_ATTEMPTS - 1):
        _post_login(client, username, "mauvais")
    _expect(_post_login(client, username, GOOD).status_code == 302, "succès avant le seuil")
    for i in range(LT.ACCOUNT_FREE_ATTEMPTS):
        res = _post_login(client, username, "mauvais")
        _expect(res.status_code == 401, f"compteur reparti de zéro (échec {i + 1})")
    _expect(_post_login(client, username, GOOD).status_code == 429, "puis délai")


def test_b8_compteur_partage_entre_instances():
    app_a, app_b = _fresh(), create_app()
    username = _member(app_a)
    client_a, client_b = app_a.test_client(), app_b.test_client()
    for i in range(LT.ACCOUNT_FREE_ATTEMPTS):
        _post_login(client_a if i % 2 else client_b, username, "mauvais")
    _expect(
        _post_login(client_b, username, GOOD).status_code == 429,
        "les échecs de deux instances (workers) s'additionnent",
    )


def test_b8_identifiant_inconnu_traite_comme_un_compte():
    app = _fresh()
    client = app.test_client()
    for _ in range(LT.ACCOUNT_FREE_ATTEMPTS):
        _post_login(client, "personne.inconnue", "x")
    _expect(
        _post_login(client, "personne.inconnue", "x").status_code == 429,
        "même comportement pour un identifiant inexistant (pas d'énumération)",
    )
    _expect(
        _post_login(client, "Personne Inconnue", "x").status_code == 429,
        "variantes de saisie normalisées vers le même compteur",
    )


def test_b8_ipv6_agregee_au_64():
    a, b = LT.ip_key("2001:db8:1:2::1"), LT.ip_key("2001:db8:1:2:ffff::9")
    _expect(a == b, "même /64 : même clé")
    _expect(LT.ip_key("2001:db8:1:3::1") != a, "autre /64 : autre clé")
    _expect(LT.ip_key("198.51.100.7") == "ip:198.51.100.7", "IPv4 : adresse entière")
    _expect(LT.ip_key("n'importe quoi") == "ip:?", "valeur invalide tolérée")


def test_b8_limite_par_ip_sur_les_echecs_seulement():
    app = _fresh()
    username = _member(app)
    client = app.test_client()
    ip = "198.51.100.200"
    for _ in range(LT.IP_FREE_ATTEMPTS + 5):
        res = _post_login(client, username, GOOD, ip=ip)
        _expect(res.status_code == 302, "des succès n'usent pas le quota IP")
    for i in range(LT.IP_FREE_ATTEMPTS):
        _post_login(client, f"inconnu{i}", "x", ip=ip)
    res = _post_login(client, username, GOOD, ip=ip)
    _expect(res.status_code == 429, f"30 échecs depuis une IP : délai ({res.status_code})")
    _expect(
        _post_login(client, username, GOOD, ip="198.51.100.201").status_code == 302,
        "autre IP non affectée",
    )


def test_b8_saisie_brute_jamais_journalisee():
    app = _fresh()
    username = _member(app)
    client = app.test_client()
    secret = "MonMotDePasse-Tapé-Ici!"
    _post_login(client, secret, "x")
    _post_login(client, username, "mauvais")
    with app.app_context():
        names = [row.name for row in db.session.scalars(select(LoginLog)).all()]
        _expect(not any("motdepasse" in n.lower() for n in names), f"saisie absente ({names})")
        _expect(any(n.startswith("inconnu:") for n in names), "identifiant inconnu : empreinte")
        _expect(username in names, "compte existant : identifiant du compte")
        keys = [row.key for row in db.session.scalars(select(LoginThrottle)).all()]
        _expect(not any(username in k for k in keys), "compteur sans identifiant en clair")


# --------------------------------------------------------------------------- B7


def _admin_client(app):
    auth_module.reset_throttle(app)
    client = app.test_client()
    res = _post_login(client, "admin", ADMIN_PASSWORD)
    _expect(res.status_code == 302, f"connexion admin ({res.status_code})")
    return client


def test_b7_duree_filtre_l_affichage_sans_purger():
    app = create_app()
    client = _admin_client(app)
    with app.app_context():
        db.session.add_all(
            [
                LoginLog(
                    name="ancienne.connexion",
                    campus="brest",
                    ip="192.0.2.1",
                    success=True,
                    created_at=utcnow() - timedelta(days=40),
                ),
                AuditLog(
                    actor="test",
                    campus="brest",
                    action="compte.creation",
                    target="cible-ancienne-xyz",
                    details="",
                    ip="192.0.2.1",
                    created_at=utcnow() - timedelta(days=40),
                ),
                AuditLog(
                    actor="test",
                    campus="brest",
                    action="compte.creation",
                    target="cible-recente-xyz",
                    details="",
                    ip="192.0.2.1",
                    created_at=utcnow(),
                ),
            ]
        )
        S.set_setting("login_logs_retention_days", 365)
        S.set_setting("audit_logs_retention_days", 365)
        db.session.commit()

    def page(tab):
        return client.get(f"/admin/journal?tab={tab}").get_data(as_text=True)

    _expect("ancienne.connexion" in page("connexions"), "365 j : ancienne connexion visible")
    _expect("cible-ancienne-xyz" in page("actions"), "365 j : ancienne action visible")

    with app.app_context():
        S.set_setting("login_logs_retention_days", 1)
        S.set_setting("audit_logs_retention_days", 1)
        db.session.commit()
    _expect("ancienne.connexion" not in page("connexions"), "1 j : connexion masquée")
    _expect("cible-ancienne-xyz" not in page("actions"), "1 j : action masquée")
    _expect("cible-recente-xyz" in page("actions"), "1 j : action récente visible")
    with app.app_context():
        _expect(
            db.session.scalar(select(LoginLog).where(LoginLog.name == "ancienne.connexion"))
            is not None,
            "rien n'a été supprimé (connexions)",
        )
        _expect(
            db.session.scalar(select(AuditLog).where(AuditLog.target == "cible-ancienne-xyz"))
            is not None,
            "rien n'a été supprimé (audit)",
        )
        S.set_setting("login_logs_retention_days", 365)
        S.set_setting("audit_logs_retention_days", 365)
        db.session.commit()
    _expect("ancienne.connexion" in page("connexions"), "retour à 365 j : connexion réapparaît")
    _expect("cible-ancienne-xyz" in page("actions"), "retour à 365 j : action réapparaît")


def _settings_post(client, **fields):
    token = _csrf(client.get("/admin/module-dev").get_data(as_text=True))
    return client.post(
        "/admin/module-dev",
        data={"action": "settings", "_csrf": token, **fields},
        follow_redirects=True,
    )


def _setting(app, key):
    with app.app_context():
        db.session.expire_all()
        return S.get_setting(key)


def test_b7_reglages_bornes():
    app = create_app()
    client = _admin_client(app)
    with app.app_context():
        S.set_setting("session_timeout_minutes", 30)
        S.set_setting("overdraft_limit_cents", 500)
        S.set_setting("max_history_days", 365)
        db.session.commit()

    for bad in ("1", "4", "0", "-5", "abc", "", "99999"):
        res = _settings_post(client, session_timeout_minutes=bad)
        _expect(res.status_code == 200, f"déconnexion {bad!r} : pas d'erreur serveur")
        _expect(_setting(app, "session_timeout_minutes") == "30", f"déconnexion {bad!r} refusée")
    _expect("Déconnexion pour inactivité" in res.get_data(as_text=True), "message d'erreur")
    _settings_post(client, session_timeout_minutes="5")
    _expect(_setting(app, "session_timeout_minutes") == "5", "plancher de 5 minutes accepté")

    for bad in ("99999999999", "-1", "abc", "1e999", "5000"):
        _settings_post(client, overdraft_limit_cents=bad)
        _expect(_setting(app, "overdraft_limit_cents") == "500", f"découvert {bad!r} refusé")
    _settings_post(client, overdraft_limit_cents="7,50")
    _expect(_setting(app, "overdraft_limit_cents") == "750", "découvert 7,50 accepté")

    _settings_post(client, max_history_days="0")
    _expect(_setting(app, "max_history_days") == "365", "historique à 0 jour refusé")
    _settings_post(client, max_history_days="30")
    _expect(_setting(app, "max_history_days") == "30", "historique à 30 jours accepté")

    _settings_post(client, session_timeout_minutes="abc", max_history_days="90")
    _expect(
        _setting(app, "max_history_days") == "90",
        "une valeur invalide n'empêche pas d'enregistrer les autres",
    )


def test_b7_plancher_de_deconnexion_applique_a_la_lecture():
    app = create_app()
    with app.app_context():
        S.set_setting("session_timeout_minutes", 1)
        db.session.commit()
        _expect(S.session_timeout_minutes() == 5, "valeur historique < 5 relevée à 5")
        S.set_setting("session_timeout_minutes", 0)
        db.session.commit()
        _expect(S.session_timeout_minutes() == 30, "0 : valeur par défaut")
    client = _admin_client(app)
    with app.app_context():
        S.set_setting("session_timeout_minutes", 1)
        db.session.commit()
    with client.session_transaction() as sess:
        sess["last_activity"] = time.time() - 4 * 60
    _expect(client.get("/admin/journal").status_code == 200, "4 min d'inactivité : session valide")
    with client.session_transaction() as sess:
        sess["last_activity"] = time.time() - 6 * 60
    res = client.get("/admin/journal")
    _expect(res.status_code == 302 and "expired" in res.headers["Location"], "6 min : expirée")
    with app.app_context():
        S.set_setting("session_timeout_minutes", 30)
        db.session.commit()


def test_b7_aucune_purge_automatique():
    app = create_app()
    client = _admin_client(app)
    with app.app_context():
        old = LoginLog(
            name="tres.ancienne",
            campus="brest",
            ip="192.0.2.9",
            success=False,
            created_at=utcnow() - timedelta(days=900),
        )
        db.session.add(old)
        S.set_setting("login_logs_retention_days", 1)
        db.session.commit()
        old_id = old.id
    client.get("/admin/journal?tab=connexions")
    _post_login(app.test_client(), "inconnu.encore", "x", ip="192.0.2.77")
    with app.app_context():
        _expect(
            db.session.get(LoginLog, old_id) is not None, "ni consultation ni connexion ne purgent"
        )
        S.set_setting("login_logs_retention_days", 90)
        db.session.commit()


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
