"""Tests du reclassement manuel des anciens articles (/admin/articles-a-reclasser).

Exécutable sans pytest : python -m tests.test_reclassement_articles
"""

import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_reclassement_"))
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
from app.models import Article, AuditLog, Transaction, TransactionLine, User  # noqa: E402
from app.services import stats_cleanup as SC  # noqa: E402
from app.utils import paris_to_utc  # noqa: E402

ADMIN_PASSWORD = "mot-de-passe-admin"
_state = {}


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _csrf(html):
    match = re.search(r'name="_csrf" value="([^"]+)"', html)
    _expect(match is not None, "jeton CSRF présent")
    return match.group(1)


def _login(client, username, password="secret123"):
    html = client.get("/connexion").get_data(as_text=True)
    res = client.post(
        "/connexion",
        data={
            "username": username,
            "password": password,
            "campus": "brest",
            "_csrf": _csrf(html),
        },
    )
    _expect(res.status_code == 302, f"connexion {username} ({res.status_code})")


def _add_line(transaction, name, kind, quantity, unit_price, article_id=None):
    transaction.lines.append(
        TransactionLine(
            article_id=article_id,
            article_name=name,
            article_type=kind,
            quantity=quantity,
            unit_price=unit_price,
            line_total=quantity * unit_price,
        )
    )


def _setup():
    if _state:
        return _state
    app = create_app()
    with app.app_context():
        mandat = User(
            name="Mandat Reclassement",
            username="reclass.mandat",
            team_status="mandat",
            team_campus="brest",
            password_hash=generate_password_hash("secret123"),
        )
        ancien = User(
            name="Ancien Reclassement",
            username="reclass.ancien",
            team_status="ancien",
            team_campus="brest",
            password_hash=generate_password_hash("secret123"),
        )
        cafe = Article(
            name="Café offert",
            article_type="snack",
            campus="brest",
            price_std=0,
            price_team=0,
            active=True,
        )
        db.session.add_all([mandat, ancien, cafe])
        db.session.flush()

        # PAF : deux variantes de casse, un compteur énorme et une ligne payante
        # à 500 € pour vérifier l'avertissement « à vérifier ».
        brest = Transaction(
            campus="brest",
            type="achat",
            created_at=paris_to_utc(datetime(2026, 5, 10, 18, 0)),
        )
        _add_line(brest, "PAF", "evenement", 125608, 0)
        _add_line(brest, "paf", "saucisson", 100, 0)
        _add_line(brest, "PAF", "evenement", 1, 50000)
        _add_line(brest, "tarte au kk", "evenement", 31667, 0)
        _add_line(brest, "Café offert", "snack", 1, 0, article_id=cafe.id)
        _add_line(brest, "Blague déjà", "blague", 5000, 0)
        brest.total = sum(line.line_total for line in brest.lines)
        db.session.add(brest)

        paris = Transaction(
            campus="paris",
            type="achat",
            created_at=paris_to_utc(datetime(2026, 5, 11, 18, 0)),
        )
        _add_line(paris, "PAF", "evenement", 200, 0)
        paris.total = 0
        db.session.add(paris)
        db.session.commit()
    _state["app"] = app
    return _state


def _admin_client():
    client = _setup()["app"].test_client()
    _login(client, "admin", ADMIN_PASSWORD)
    return client


def _page(client, query=""):
    res = client.get(f"/admin/articles-a-reclasser?campus=brest{query}")
    _expect(res.status_code == 200, f"page de reclassement ({res.status_code})")
    return res.get_data(as_text=True)


def test_reclassement_articles():
    state = _setup()
    app = state["app"]

    # Accès : anonyme redirigé, membre non mandat refusé.
    anonymous = app.test_client()
    _expect(
        anonymous.get("/admin/articles-a-reclasser").status_code == 302,
        "anonyme redirigé vers la connexion",
    )
    member = app.test_client()
    _login(member, "reclass.mandat")
    _expect(
        member.get("/admin/articles-a-reclasser").status_code == 200,
        "mandat autorisé",
    )
    former = app.test_client()
    _login(former, "reclass.ancien")
    _expect(
        former.get("/admin/articles-a-reclasser").status_code == 403,
        "ancien membre refusé",
    )

    with app.app_context():
        all_brest = {c["name"]: c for c in SC.candidates("brest", show_all=True)}
        default_brest = {c["name"]: c for c in SC.candidates("brest")}
        paf = all_brest["PAF"]
        _expect(paf["names"] == ["PAF", "paf"], "variantes de casse groupées")
        _expect(paf["zero_max_qty"] == 125608, "quantité maximale détectée")
        _expect(paf["revenue"] == 50000, "recettes du groupe")
        _expect("Évènement/Soirée" in paf["type_labels"], "type évènement listé")
        _expect("Saucisson" in paf["type_labels"], "type saucisson listé")
        _expect("Café offert" in all_brest, "offert visible en mode complet")
        _expect("Café offert" not in default_brest, "offert hors seuil par défaut")
        _expect("Blague déjà" in all_brest, "blague déjà typée visible en mode complet")
        _expect("Blague déjà" not in default_brest, "blague déjà typée exclue par défaut")
        _expect("PAF" in default_brest and "tarte au kk" in default_brest, "suspects listés")
        search = SC.candidates("brest", search="tarte")
        _expect([c["name"] for c in search] == ["tarte au kk"], "recherche par nom")
        paris = {c["name"]: c for c in SC.candidates("paris", show_all=True)}
        _expect(paris["PAF"]["qty"] == 200, "campus paris isolé")
        _expect("tarte au kk" not in paris, "pas de fuite entre campus")

    client = _admin_client()
    html = _page(client)
    _expect("PAF" in html and "tarte au kk" in html, "articles suspects affichés")
    _expect("500,00 €" in html and "à vérifier" in html, "recettes signalées")
    _expect("Café offert" not in html, "offerts masqués par défaut")
    _expect("Blague déjà" not in html, "déjà blague masquée")
    full = _page(client, "&tous=1")
    _expect("Café offert" in full, "offerts visibles avec tous=1")
    _expect("Blague déjà" in full, "blagues déjà typées visibles avec tous=1")
    filtered = _page(client, "&q=tarte")
    _expect("tarte au kk" in filtered, "filtre de recherche serveur")
    _expect(len(re.findall(r'name="type__', filtered)) == 1, "une seule ligne filtrée")

    # Un type inconnu est ignoré : aucune ligne ne doit changer.
    with app.app_context():
        tarte = next(c for c in SC.candidates("brest", show_all=True) if c["name"] == "tarte au kk")
        tarte_token = tarte["token"]
    res = client.post(
        "/admin/articles-a-reclasser?campus=brest",
        data={"type__" + tarte_token: "type-inconnu", "_csrf": _csrf(html)},
        follow_redirects=True,
    )
    _expect(res.status_code == 200, "POST type inconnu accepté sans erreur")
    _expect("Aucun type sélectionné" in res.get_data(as_text=True), "message type inconnu")
    with app.app_context():
        _expect(
            db.session.scalar(
                db.select(TransactionLine.article_type)
                .where(TransactionLine.article_name == "tarte au kk")
                .limit(1)
            )
            == "evenement",
            "type inconnu sans effet",
        )

    # Reclassement réel de PAF, y compris les variantes de casse et la ligne payante.
    res = client.post(
        "/admin/articles-a-reclasser?campus=brest",
        data={"type__" + paf["token"]: "blague", "_csrf": _csrf(html)},
        follow_redirects=True,
    )
    text = res.get_data(as_text=True)
    _expect(res.status_code == 200, "POST reclassement")
    _expect("1 article(s) reclassé(s)" in text, "message de succès")
    with app.app_context():
        brest_types = db.session.scalars(
            db.select(TransactionLine.article_type).where(
                TransactionLine.article_name.in_(("PAF", "paf")),
                TransactionLine.transaction_id.in_(
                    db.select(Transaction.id).where(Transaction.campus == "brest")
                ),
            )
        ).all()
        _expect(brest_types and set(brest_types) == {"blague"}, "toutes les lignes PAF reclassées")
        paris_type = db.session.scalar(
            db.select(TransactionLine.article_type)
            .join(Transaction, TransactionLine.transaction_id == Transaction.id)
            .where(
                Transaction.campus == "paris",
                TransactionLine.article_name == "PAF",
            )
            .limit(1)
        )
        _expect(paris_type == "evenement", "campus paris inchangé")
        tarte_type = db.session.scalar(
            db.select(TransactionLine.article_type)
            .where(TransactionLine.article_name == "tarte au kk")
            .limit(1)
        )
        _expect(tarte_type == "evenement", "tarte inchangée")
        audit = db.session.scalars(
            db.select(AuditLog).where(AuditLog.action == "stats.reclassement_article")
        ).all()
        _expect(len(audit) == 1, "action journalisée")
        _expect("Blague" in audit[0].details, "nouveau type dans le journal")
        default_after = SC.candidates("brest")
        _expect(
            not any(c["name"] == "PAF" for c in default_after),
            "PAF disparaît des suspects",
        )
        all_after = SC.candidates("brest", show_all=True)
        paf_after = next(c for c in all_after if c["name"] == "PAF")
        _expect(paf_after["types"] == ["blague"], "PAF reste corrigeable en mode complet")

    # Un type autre que Blague marque aussi l'article comme traité : il quitte
    # la liste par défaut mais reste accessible en mode complet.
    res = client.post(
        "/admin/articles-a-reclasser?campus=brest",
        data={"type__" + tarte_token: "snack", "_csrf": _csrf(html)},
        follow_redirects=True,
    )
    _expect("1 article(s) reclassé(s)" in res.get_data(as_text=True), "second reclassement")
    with app.app_context():
        tarte_types = db.session.scalars(
            db.select(TransactionLine.article_type).where(
                TransactionLine.article_name == "tarte au kk"
            )
        ).all()
        _expect(set(tarte_types) == {"snack"}, "tarte reclassée en snack")
        _expect(
            not any(c["name"] == "tarte au kk" for c in SC.candidates("brest")),
            "article traité retiré de la liste par défaut",
        )
        tarte_after = next(
            c for c in SC.candidates("brest", show_all=True) if c["name"] == "tarte au kk"
        )
        _expect(tarte_after["types"] == ["snack"], "article traité visible en mode complet")
        audit_count = db.session.scalar(
            db.select(db.func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "stats.reclassement_article")
        )
        _expect(audit_count == 2, "deux actions journalisées")

    # Un mandat brestois ne peut pas écrire sur Paris (lecture seule).
    paris_token = None
    with app.app_context():
        paris_paf = next(c for c in SC.candidates("paris", show_all=True) if c["name"] == "PAF")
        paris_token = paris_paf["token"]
    mandat_page = member.get("/admin/articles-a-reclasser?campus=paris").get_data(as_text=True)
    _expect("lecture seule" in mandat_page, "bandeau lecture seule")
    res = member.post(
        "/admin/articles-a-reclasser?campus=paris",
        data={"type__" + paris_token: "blague", "_csrf": _csrf(mandat_page)},
    )
    _expect(res.status_code == 403, "écriture inter-campus refusée")


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
