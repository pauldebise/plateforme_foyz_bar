"""Tests du journal d'audit des actions d'administration (P10-1).

Exécutable sans pytest : python -m tests.test_audit

Couvre :
- enregistrement des actions sensibles (comptes, équipe, articles, réglages,
  liens) avec acteur, cible, IP ;
- aucune fuite de secret (un mot de passe redéfini n'apparaît jamais) ;
- consultation /admin/audit (filtres action et acteur/cible, pagination) ;
- accès réservé aux mandats (un ancien membre reçoit 403) ;
- purge selon la rétention (CLI, --dry-run).
"""

import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_audit_"))
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
from app.models import AuditLog, User  # noqa: E402
from app.utils import utcnow  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"


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
    return res


def test_actions_enregistrees():
    app = create_app()
    client = app.test_client()
    _expect(_login(client).status_code == 302, "connexion administrateur")
    token = _csrf(client, "/admin/comptes")

    res = client.post(
        "/admin/comptes/nouveau",
        data={
            "name": "Jeanne Audit",
            "username": "jeanne.audit",
            "promotion": "2026",
            "_csrf": token,
        },
    )
    _expect(res.status_code == 302, "création de compte")
    with app.app_context():
        user = db.session.query(User).filter(User.username == "jeanne.audit").first()
        _expect(user is not None, "compte créé")
        user_id = user.id
        created = (
            db.session.query(AuditLog)
            .filter(AuditLog.action == "compte.creation")
            .order_by(AuditLog.id.desc())
            .first()
        )
        _expect(created is not None, "création journalisée")
        _expect("Jeanne Audit" in created.target, "cible = compte créé")
        _expect(created.actor == "admin", "acteur journalisé")
        _expect(created.ip, "IP journalisée")
        _expect(created.campus == "brest", "campus journalisé")

    # Modification : blacklist activée
    res = client.post(
        f"/admin/comptes/{user_id}",
        data={
            "name": "Jeanne Audit",
            "username": "jeanne.audit",
            "promotion": "2026",
            "blacklist": "on",
            "_csrf": token,
        },
    )
    _expect(res.status_code == 302, "modification de compte")
    with app.app_context():
        modified = (
            db.session.query(AuditLog)
            .filter(AuditLog.action == "compte.modification")
            .order_by(AuditLog.id.desc())
            .first()
        )
        _expect(modified is not None, "modification journalisée")
        _expect("blacklist : oui" in modified.details, "changement décrit")

    # Suppression (mot de passe administrateur exigé)
    res = client.post(
        f"/admin/comptes/{user_id}/supprimer",
        data={"admin_password": ADMIN_PASSWORD, "_csrf": token},
    )
    _expect(res.status_code == 302, "suppression de compte")
    with app.app_context():
        _expect(
            db.session.query(AuditLog).filter(AuditLog.action == "compte.suppression").count() == 1,
            "suppression journalisée",
        )

    # Article
    client.post(
        "/admin/articles/nouveau",
        data={
            "name": "Pinte test audit",
            "article_type": "biere",
            "price_std_brest": "4.50",
            "price_team_brest": "3.50",
            "active": "on",
            "_csrf": token,
        },
    )
    with app.app_context():
        article = db.session.query(AuditLog).filter(AuditLog.action == "article.creation").first()
        _expect(article is not None and "Pinte test audit" in article.target, "article journalisé")

    # Réglages + lien
    client.post(
        "/admin/module-dev",
        data={"action": "settings", "site_name": "Foy'z audit", "_csrf": token},
    )
    client.post(
        "/admin/module-dev",
        data={
            "action": "add_link",
            "label": "Site ENSTA",
            "url": "https://ensta.fr",
            "_csrf": token,
        },
    )
    client.post(
        "/admin/module-dev",
        data={"action": "settings", "site_name": "Foy'z", "_csrf": token},
    )
    with app.app_context():
        _expect(
            db.session.query(AuditLog).filter(AuditLog.action == "reglages.modification").count()
            >= 2,
            "réglages journalisés",
        )
        link = db.session.query(AuditLog).filter(AuditLog.action == "lien.ajout").first()
        _expect(link is not None and link.target == "Site ENSTA", "lien journalisé")


def test_mot_de_passe_jamais_journalise():
    app = create_app()
    client = app.test_client()
    _expect(_login(client).status_code == 302, "connexion administrateur")
    token = _csrf(client, "/admin/equipe")
    with app.app_context():
        member = User(name="Marc Membre", username="marc.membre")
        db.session.add(member)
        db.session.commit()
        member_id = member.id
    secret = "MotDePasseTresSecret123!"
    client.post(
        f"/admin/equipe/{member_id}",
        data={"team_status": "mandat", "password": secret, "_csrf": token},
    )
    with app.app_context():
        row = (
            db.session.query(AuditLog)
            .filter(AuditLog.action == "equipe.modification")
            .order_by(AuditLog.id.desc())
            .first()
        )
        _expect(row is not None, "changement d'équipe journalisé")
        _expect("mot de passe redéfini" in row.details, "redéfinition mentionnée")
        _expect(secret not in row.details and secret not in row.target, "secret absent du journal")


def test_page_audit_et_filtres():
    app = create_app()
    client = app.test_client()
    _expect(_login(client).status_code == 302, "connexion administrateur")
    page = client.get("/admin/audit").get_data(as_text=True)
    _expect("Journal d'audit" in page, "page accessible")
    _expect("Action" in page and "Acteur" in page, "filtres présents")

    filtered = client.get("/admin/audit?action=reglages.modification").get_data(as_text=True)
    _expect("Modification des réglages" in filtered, "filtre par action")
    _expect("Jeanne Audit" not in filtered, "actions non concernées filtrées")

    client.get("/admin/audit?page=abc")
    _expect(client.get("/admin/audit?page=abc").status_code == 200, "pagination tolérante")
    _expect(
        client.get("/admin/audit?from=pas-une-date").status_code == 200, "date invalide tolérée"
    )


def test_acces_reserve_aux_mandats():
    app = create_app()
    with app.app_context():
        old = User(
            name="Ancien Membre",
            username="ancien.audit",
            team_status="ancien",
            team_campus="brest",
            password_hash=generate_password_hash("secret123"),
        )
        db.session.add(old)
        db.session.commit()
    client = app.test_client()
    _expect(
        _login(client, "ancien.audit", "secret123").status_code == 302, "connexion ancien membre"
    )
    _expect(client.get("/admin/audit").status_code == 403, "audit interdit aux anciens membres")


def test_purge_audit():
    app = create_app()
    with app.app_context():
        old = AuditLog(
            actor="test",
            campus="brest",
            action="compte.creation",
            target="vieux",
            details="",
            ip="127.0.0.1",
            created_at=utcnow() - timedelta(days=800),
        )
        recent = AuditLog(
            actor="test",
            campus="brest",
            action="compte.creation",
            target="recent",
            details="",
            ip="127.0.0.1",
            created_at=utcnow(),
        )
        db.session.add_all([old, recent])
        db.session.commit()
        old_id = old.id

    runner = app.test_cli_runner()
    result = runner.invoke(args=["purge-audit", "--days", "365", "--dry-run"])
    _expect(result.exit_code == 0, "purge-audit --dry-run")
    with app.app_context():
        _expect(db.session.get(AuditLog, old_id) is not None, "dry-run ne supprime rien")

    result = runner.invoke(args=["purge-audit", "--days", "365"])
    _expect(result.exit_code == 0, "purge-audit")
    with app.app_context():
        _expect(db.session.get(AuditLog, old_id) is None, "entrée ancienne purgée")
        _expect(
            db.session.query(AuditLog).filter(AuditLog.target == "recent").count() == 1,
            "entrée récente conservée",
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
