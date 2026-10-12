"""Trésorerie : tableau (rechargements / ventes), graphique en barres côte à côte
et détail par type d'article conservé dans les CSV.

Exécutable sans pytest : python -m tests.test_tresorerie
"""

import csv
import io
import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="foyz_tresorerie_"))
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
from app.models import Article, Event, User  # noqa: E402
from app.services import transactions as T  # noqa: E402
from app.services.treasury import treasury  # noqa: E402
from app.utils import utcnow  # noqa: E402

STATIC = ROOT / "app" / "static"
_state = {}


def _expect(cond, label):
    if not cond:
        raise AssertionError(f"échec : {label}")


def _setup():
    if _state:
        return _state
    app = create_app()
    with app.app_context():
        student = User(
            name="Treso Etudiant",
            username="treso.etudiant",
            promotion=2027,
            password_hash=generate_password_hash("x"),
        )
        db.session.add(student)
        arts = {
            key: Article(name=f"Treso {key}", article_type=key, campus="brest", price_std=100)
            for key in ("biere", "cidre", "evenement", "blague")
        }
        db.session.add_all(arts.values())
        event = Event(
            name="Treso Soiree",
            campus="brest",
            starts_at=utcnow() - timedelta(hours=1),
            ends_at=utcnow() + timedelta(hours=1),
            token="treso-token",
        )
        db.session.add(event)
        db.session.flush()
        student.wallet("brest").balance = 100_000
        db.session.commit()

        def reload(amount, method):
            T.create_reload(
                operator_label="t",
                campus="brest",
                user=db.session.get(User, student.id),
                amount_cents=amount,
                payment_method=method,
            )

        def sell(key, qty, **extra):
            T.create_purchase(
                operator_label="t",
                campus="brest",
                items=[{"article_id": arts[key].id, "quantity": qty}],
                direct=True,
                payment_method="cb",
                **extra,
            )

        reload(1000, "cb")
        reload(500, "especes")
        reload(300, "lydia")
        reload(200, "helloasso")
        sell("biere", 3)  # 300 hors évènements
        sell("cidre", 2)  # 200 hors évènements
        sell("blague", 1)  # 100 hors évènements
        sell("evenement", 7)  # 700 : type évènement/soirée
        sell("biere", 4, event_id=event.id)  # 400 : vendu via la passerelle d'un événement
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
    _state.update(app=app, client=client)
    return _state


def _current_month(report):
    return report[-1]


def test_calcul_rechargements_et_ventes():
    s = _setup()
    with s["app"].app_context():
        e = _current_month(treasury("brest"))
    _expect(e["reloads_total"] == 2000, f"rechargements {e['reloads_total']}")
    _expect(
        e["reloads_by_method"] == {"cb": 1000, "especes": 500, "lydia": 300, "helloasso": 200},
        f"rechargements par moyen {e['reloads_by_method']}",
    )
    _expect(e["sales_other"] == 600, f"ventes hors évènements/soirées {e['sales_other']}")
    _expect(e["sales_events"] == 1100, f"ventes évènements/soirées {e['sales_events']}")
    _expect(e["sales_total"] == 1700, f"total des ventes {e['sales_total']}")
    _expect(
        sum(e["sales_by_type"].values()) == e["sales_total"],
        "le détail par type se somme au total des ventes",
    )
    _expect(e["sales_by_type"]["evenement"] == 700, "type évènement/soirée")
    _expect(e["sales_by_type"]["biere"] == 700, "bière : 300 + 400 via l'événement")
    _expect(e["sales_by_type"]["blague"] == 100, "type blague")


def test_tableau_sans_detail_par_type():
    s = _setup()
    page = s["client"].get("/equipe/tresorerie").get_data(as_text=True)
    head = page[page.index("<thead") : page.index("</thead>")]
    for label in ("Rechargements", "Carte Bancaire", "Espèces", "Lydia", "HelloAsso"):
        _expect(label in head, f"colonne {label}")
    _expect("Hors évènements/soirées" in head, "colonne ventes ordinaires")
    _expect("Évènements/soirées" in head, "colonne ventes évènements/soirées")
    for label in ("Bière", "Cidre", "Vin", "Snacks", "Saucisson", "Blague", "Total entrées"):
        _expect(label not in head, f"plus de colonne {label}")
    _expect("Total entrées" not in page, "plus de total des entrées")
    body = page[page.index("<tbody") : page.index("</tbody>")]
    _expect(body.count("<tr>") == 12, "douze mois")
    last_row = body[body.rindex("<tr>") :]
    cells = re.findall(r"<td[^>]*>\s*([^<]*?)\s*</td>", last_row)[:7]
    _expect(
        cells[1:] == ["10,00 €", "3,00 €", "5,00 €", "2,00 €", "6,00 €", "11,00 €"],
        f"montants du mois courant : {cells}",
    )


def test_graphique_barres_cote_a_cote():
    js = (STATIC / "js" / "tresorerie.js").read_text(encoding="utf-8")
    _expect("type: 'line'" not in js, "plus de courbe")
    _expect("y2" not in js, "un seul axe vertical")
    _expect("stack: 'rechargements'" in js and "stack: 'ventes'" in js, "deux barres par mois")
    _expect("reloads_total" in js and "sales_other" in js and "sales_events" in js, "séries")
    page = _setup()["client"].get("/equipe/tresorerie").get_data(as_text=True)
    _expect('id="treasury-data"' in page, "données du graphique")


def _csv(client, url):
    res = client.get(url)
    _expect(res.status_code == 200, f"{url} : {res.status_code}")
    text = res.get_data(as_text=True).lstrip("\ufeff")
    return list(csv.reader(io.StringIO(text), delimiter=";"))


def test_csv_conservent_le_detail_par_type():
    s = _setup()
    client = s["client"]
    rows = _csv(client, "/equipe/tresorerie/export/annuel?campus=brest")
    header = rows[0]
    for label in ("Bière", "Vin", "Cidre", "Snacks", "Saucisson", "Évènement/Soirée", "Blague"):
        _expect(label in header, f"annuel : détail {label}")
    for label in (
        "Carte Bancaire",
        "Rechargements (total)",
        "Ventes hors évènements/soirées",
        "Ventes évènements/soirées",
        "Ventes (total)",
    ):
        _expect(label in header, f"annuel : colonne {label}")
    _expect("Total entrées" not in header, "annuel : plus de total des entrées")
    current = rows[12]
    col = {name: i for i, name in enumerate(header)}
    _expect(current[col["Bière"]] == "7.00", f"annuel : bière {current[col['Bière']]}")
    _expect(current[col["Cidre"]] == "2.00", "annuel : cidre")
    _expect(current[col["Ventes hors évènements/soirées"]] == "6.00", "annuel : ventes ordinaires")
    _expect(current[col["Ventes évènements/soirées"]] == "11.00", "annuel : évènements")
    _expect(current[col["Ventes (total)"]] == "17.00", "annuel : total ventes")
    _expect(current[col["Rechargements (total)"]] == "20.00", "annuel : total rechargements")
    total = rows[-1]
    _expect(
        total[0].startswith("TOTAL") and total[col["Ventes (total)"]] == "17.00", "annuel : total"
    )
    _expect(all(len(r) == len(header) for r in rows), "annuel : lignes de même largeur")

    from app.utils import to_paris

    now = to_paris(utcnow())
    monthly = _csv(
        client, f"/equipe/tresorerie/export/mensuel?campus=brest&year={now.year}&month={now.month}"
    )
    labels = {(r[0], r[1]): r[2] for r in monthly if len(r) == 3}
    _expect(labels[("Rechargements", "Carte Bancaire")] == "10.00", "mensuel : CB")
    _expect(labels[("Rechargements", "Total rechargements")] == "20.00", "mensuel : rechargements")
    _expect(labels[("Ventes par type", "Bière")] == "7.00", "mensuel : détail bière")
    _expect(labels[("Ventes par type", "Évènement/Soirée")] == "7.00", "mensuel : détail évènement")
    _expect(labels[("Ventes", "Hors évènements/soirées")] == "6.00", "mensuel : ventes ordinaires")
    _expect(labels[("Ventes", "Évènements/soirées")] == "11.00", "mensuel : évènements")


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
