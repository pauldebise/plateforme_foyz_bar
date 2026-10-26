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
import time
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
from app.models import Article, AuditLog, Note, Transaction, User  # noqa: E402
from app.routes import auth as auth_module  # noqa: E402
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


def test_b_notes_signatures():
    """Post-it publics : nom réel ; notes privées : surnom conservé."""
    app = create_app()
    with app.app_context():
        set_setting("max_postits_private", "50")
        set_setting("max_postits_public", "50")
        auteur = User(
            name="Nom Public",
            nickname="SurnomPublic",
            username="notes.public",
            team_status="mandat",
            team_campus="brest",
        )
        db.session.add(auteur)
        db.session.flush()
        db.session.add_all(
            [
                Note(
                    content="Post-it public historique",
                    is_public=True,
                    author_id=auteur.id,
                    author_name=auteur.display_name,
                ),
                Note(
                    content="Post-it public supprime",
                    is_public=True,
                    author_id=None,
                    author_name=auteur.display_name,
                ),
            ]
        )
        db.session.commit()
        auteur_id = auteur.id
        nom = auteur.name
        signature_privee = auteur.display_name

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_id"] = auteur_id
        sess["campus"] = "brest"
        sess["last_activity"] = time.time()
    token = _csrf(client, "/equipe/notes")
    for scope, content in (
        ("public", "Post-it public cree"),
        ("private", "Post-it prive cree"),
    ):
        res = client.post(
            "/equipe/notes/action",
            data={"action": "create", "scope": scope, "content": content, "_csrf": token},
        )
        _expect(res.status_code == 302, f"création du post-it {scope}")

    with app.app_context():
        public_note = db.session.query(Note).filter_by(content="Post-it public cree").one()
        private_note = db.session.query(Note).filter_by(content="Post-it prive cree").one()
        _expect(public_note.author_name == nom, "post-it public : nom réel enregistré")
        _expect(private_note.author_name == signature_privee, "note privée : surnom conservé")

    home = client.get("/").get_data(as_text=True)
    _expect("Nom Public" in home, "post-it public : nom réel affiché")
    _expect("SurnomPublic" not in home, "post-it public : surnom absent de la page publique")

    team = client.get("/equipe/notes").get_data(as_text=True)
    public_block = team.split("Post-it public cree", 1)[1][:250]
    _expect("Nom Public" in public_block, "post-it public : nom réel dans l'équipe")
    _expect("SurnomPublic" not in public_block, "post-it public : surnom absent dans l'équipe")
    private_block = team.split("Post-it prive cree", 1)[1][:250]
    _expect("SurnomPublic" in private_block, "note privée : surnom conservé à l'affichage")


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


def test_d_article_prive():
    """Article privé : masqué du catalogue public, encaissable en caisse,
    bascule depuis l'onglet article."""
    app = create_app()
    with app.app_context():
        public = Article(
            name="Limonade publique",
            article_type="snack",
            campus="brest",
            price_std=200,
            price_team=150,
            active=True,
        )
        prive = Article(
            name="Cocktail prive",
            article_type="snack",
            campus="brest",
            price_std=500,
            price_team=400,
            active=True,
            is_private=True,
        )
        db.session.add_all([public, prive])
        db.session.commit()
        prive_id = prive.id

    client = app.test_client()
    catalogue = client.get("/catalogue?campus=brest").get_data(as_text=True)
    _expect("Limonade publique" in catalogue, "article public visible au catalogue")
    _expect("Cocktail prive" not in catalogue, "article privé masqué du catalogue public")

    token = _login(client)
    payment = client.get("/equipe/paiement").get_data(as_text=True)
    _expect("Cocktail prive" in payment, "article privé toujours encaissable")

    # Bascule privé -> public depuis l'onglet article.
    client.post(
        f"/admin/articles/{prive_id}",
        data={
            "name": "Cocktail prive",
            "article_type": "snack",
            "price_std": "5.00",
            "price_team": "4.00",
            "active": "on",
            "_csrf": token,
        },
    )
    with app.app_context():
        _expect(not db.session.get(Article, prive_id).is_private, "article repassé public")
    _expect(
        "Cocktail prive" in client.get("/catalogue?campus=brest").get_data(as_text=True),
        "article repassé public : visible au catalogue",
    )

    # Bascule public -> privé.
    client.post(
        f"/admin/articles/{prive_id}",
        data={
            "name": "Cocktail prive",
            "article_type": "snack",
            "price_std": "5.00",
            "price_team": "4.00",
            "active": "on",
            "is_private": "on",
            "_csrf": token,
        },
    )
    with app.app_context():
        _expect(db.session.get(Article, prive_id).is_private, "article repassé privé")
    _expect(
        "Cocktail prive" not in client.get("/catalogue?campus=brest").get_data(as_text=True),
        "article rebasculé privé : masqué du catalogue",
    )


def test_d2_article_blague():
    """Type « Blague » : encaissable en caisse, masqué du catalogue public."""
    app = create_app()
    with app.app_context():
        serieux = Article(
            name="Pinte serieuse test",
            article_type="biere",
            campus="brest",
            price_std=300,
            price_team=250,
            active=True,
        )
        blague = Article(
            name="TA GUEUELE CHOIMET",
            article_type="blague",
            campus="brest",
            price_std=100,
            price_team=100,
            active=True,
        )
        db.session.add_all([serieux, blague])
        db.session.commit()
        blague_id = blague.id

    client = app.test_client()
    catalogue = client.get("/catalogue?campus=brest").get_data(as_text=True)
    _expect("Pinte serieuse test" in catalogue, "article sérieux visible au catalogue")
    _expect("TA GUEUELE CHOIMET" not in catalogue, "article blague masqué du catalogue public")

    # Le limiteur de connexions est partagé par tout le processus de test
    # (même IP) : purge locale pour rester sous le seuil, comme les suites
    # sécurité/hardening.
    auth_module.reset_throttle(app)
    _login(client)
    payment = client.get("/equipe/paiement").get_data(as_text=True)
    _expect("TA GUEUELE CHOIMET" in payment, "article blague encaissable en caisse")

    form = client.get("/admin/articles/nouveau").get_data(as_text=True)
    _expect('value="blague"' in form, "type Blague proposé dans le formulaire article")

    with app.app_context():
        t = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": blague_id, "quantity": 1}],
            direct=True,
            payment_method="especes",
        )
        _expect(t.total == 100, f"article blague encaissé (total {t.total})")

    stats = client.get("/equipe/statistiques").get_data(as_text=True)
    _expect("Blague" in stats, "catégorie Blague proposée dans les statistiques")
    rapport = client.get("/equipe/tresorerie/export/annuel?campus=brest").get_data(as_text=True)
    _expect("Blague" in rapport, "colonne Blague présente dans le rapport annuel de trésorerie")


def test_e_recherche_articles():
    """Barre de recherche de l'onglet article : filtre par nom, campus conservé."""
    app = create_app()
    with app.app_context():
        db.session.add_all(
            [
                Article(
                    name="Pinte blonde",
                    article_type="biere",
                    campus="brest",
                    price_std=450,
                    price_team=350,
                    active=True,
                ),
                Article(
                    name="Coca",
                    article_type="snack",
                    campus="brest",
                    price_std=200,
                    price_team=150,
                    active=True,
                ),
            ]
        )
        db.session.commit()

    client = app.test_client()
    _login(client)

    page = client.get("/admin/articles?campus=brest&q=pinte").get_data(as_text=True)
    _expect("Pinte blonde" in page, "article correspondant affiché")
    _expect(">Coca<" not in page, "article non correspondant masqué")
    _expect('<input type="hidden" name="campus" value="brest">' in page, "campus conservé")

    empty = client.get("/admin/articles?campus=brest&q=zzz").get_data(as_text=True)
    _expect("Aucun article ne correspond à « zzz »" in empty, "message si aucun résultat")

    full = client.get("/admin/articles?campus=brest").get_data(as_text=True)
    _expect("Pinte blonde" in full and ">Coca<" in full, "liste complète sans recherche")


def test_f_suppression_article_conserve_historique():
    """Suppression d'un article : retiré de la base, ventes passées intactes."""
    app = create_app()
    with app.app_context():
        article = Article(
            name="Pinte a supprimer",
            article_type="biere",
            campus="brest",
            price_std=300,
            price_team=250,
            active=True,
        )
        user = User(name="Élève Historique", username="ux.historique")
        db.session.add(user)
        db.session.flush()
        user.wallet("brest").balance = 1000
        db.session.add(article)
        db.session.commit()
        article_id = article.id
        txn = T.create_purchase(
            operator_label="test",
            campus="brest",
            items=[{"article_id": article_id, "quantity": 1}],
            contributor_ids=[user.id],
        )
        txn_id = txn.id

    client = app.test_client()
    token = _login(client)
    res = client.post(f"/admin/articles/{article_id}/supprimer", data={"_csrf": token})
    _expect(res.status_code == 302, "suppression acceptée")

    with app.app_context():
        _expect(db.session.get(Article, article_id) is None, "article supprimé de la base")
        line = db.session.get(Transaction, txn_id).lines[0]
        _expect(line.article_id is None, "ligne d'historique détachée de l'article")
        _expect(line.article_name == "Pinte a supprimer", "nom conservé dans l'historique")
        _expect(
            db.session.query(AuditLog).filter(AuditLog.action == "article.suppression").count()
            == 1,
            "suppression journalisée",
        )


def test_g_selection_etudiant_operations():
    """Rechargement/retrait : étudiant sélectionné présenté comme au paiement."""
    app = create_app()
    client = app.test_client()
    _login(client)
    for path in ("/equipe/rechargement", "/equipe/retrait"):
        page = client.get(path).get_data(as_text=True)
        _expect('id="user-list"' in page, f"carte de sélection sur {path}")
        _expect('data-list="user-list"' in page, f"recherche reliée à la carte sur {path}")
    op_js = (STATIC / "js" / "operation.js").read_text(encoding="utf-8")
    _expect(
        "list-group-item d-flex justify-content-between" in op_js,
        "carte de liste identique au paiement/transfert",
    )
    _expect("bi-person-circle" in op_js, "icône étudiant sur la carte")
    # Le résultat de recherche porte déjà solde/badges : pas de requête en plus.
    _expect("/api/wallet/" not in op_js, "sélection sans appel portefeuille redondant")
    payment_js = (STATIC / "js" / "payment.js").read_text(encoding="utf-8")
    _expect("/api/wallet/" not in payment_js, "paiement sans appel portefeuille redondant")
    app_js = (STATIC / "js" / "app.js").read_text(encoding="utf-8")
    # Réactivité caisse : plus de debounce, la requête en vol est annulée à
    # chaque frappe (seule la réponse au dernier état du champ s'affiche).
    _expect("}, 120);" not in app_js, "recherche étudiant sans debounce")
    _expect("AbortController" in app_js, "requêtes obsolètes annulées")
    _expect("cancelPending()" in app_js, "annulation branchée sur les frappes")


def test_h_raccourcis_caisse():
    """Caisse : Échap vide la commande, +/− ajustent le dernier article ajouté."""
    app = create_app()
    client = app.test_client()
    _login(client)
    page = client.get("/equipe/paiement").get_data(as_text=True)
    _expect("js/payment.js" in page, "script de caisse chargé")
    payment_js = (STATIC / "js" / "payment.js").read_text(encoding="utf-8")
    _expect("document.addEventListener('keydown'" in payment_js, "écoute clavier globale")
    _expect("e.key === 'Escape'" in payment_js, "touche Échap gérée")
    _expect("function clearOrder()" in payment_js, "Échap vide la commande en cours")
    _expect("lastArticleId = article.id" in payment_js, "dernier article ajouté suivi")
    _expect(
        "e.key === '+' || e.key === '='" in payment_js,
        "touche + gérée",
    )
    _expect("e.key === '-'" in payment_js, "touche − gérée")
    _expect("changeLastArticleQuantity(1)" in payment_js, "augmenter la quantité")
    _expect("changeLastArticleQuantity(-1)" in payment_js, "diminuer la quantité")
    _expect("isTypingTarget(e.target)" in payment_js, "saisie texte préservée")
    _expect("modalOpen()" in payment_js, "raccourcis suspendus pendant une modale")


def test_enchainement_apres_encaissement():
    """Après un encaissement : recherche d'articles vidée, curseur dans la
    recherche d'étudiant, soldes affichés sans bloquer la saisie suivante."""
    app = create_app()
    client = app.test_client()
    _login(client)
    js = (STATIC / "js" / "payment.js").read_text(encoding="utf-8")
    reset = js[js.index("function resetCartState()") : js.index("function clearOrder()")]
    _expect("els.catalogSearch.value = ''" in reset, "recherche d'articles vidée après la commande")
    _expect("focusStudentSearch();" in reset, "focus rendu à la recherche d'étudiant")
    _expect("els.search.focus(" in js, "focus de la recherche d'étudiant")
    _expect("focus: false" in js, "la confirmation ne prend pas le focus")
    _expect(
        ".modal.show:not(#success-modal)" in js, "la confirmation ne suspend pas les raccourcis"
    )
    _expect("showSuccess(res.total, res.balances)" in js, "soldes transmis à la confirmation")
    _expect("balanceBadge(b.balance, 'fs-6')" in js, "solde négatif en rouge dans la confirmation")
    for url in ("/equipe/paiement",):
        page = client.get(url).get_data(as_text=True)
        _expect('id="success-balances"' in page, "zone des soldes dans la confirmation")


def test_i_modale_decouvert():
    """Modale découvert : mot de passe focalisé à l'ouverture, Entrée valide."""
    app = create_app()
    client = app.test_client()
    _login(client)
    page = client.get("/equipe/paiement").get_data(as_text=True)
    _expect('id="admin-form"' in page, "modale découvert dans un formulaire")
    _expect('id="admin-password-input"' in page, "champ mot de passe présent")
    _expect('type="submit"' in page, "validation par soumission (Entrée, clavier mobile)")

    gateway_tpl = (ROOT / "app" / "templates" / "gateway" / "paiement.html").read_text(
        encoding="utf-8"
    )
    _expect('id="admin-form"' in gateway_tpl, "même mécanique sur la passerelle")
    _expect('type="submit"' in gateway_tpl, "soumission aussi sur la passerelle")

    payment_js = (STATIC / "js" / "payment.js").read_text(encoding="utf-8")
    _expect("'shown.bs.modal'" in payment_js, "focus à l'ouverture de la modale")
    _expect("$('admin-password-input').focus()" in payment_js, "autofocus du mot de passe")
    _expect("$('admin-form').addEventListener('submit'" in payment_js, "Entrée valide la saisie")


def test_j_recherche_articles_videe_a_l_ajout():
    """Ajout d'un article — clic souris comme Entrée : la recherche est vidée
    pour taper directement l'article suivant."""
    app = create_app()
    client = app.test_client()
    _login(client)
    page = client.get("/equipe/paiement").get_data(as_text=True)
    _expect('id="catalog-search"' in page, "champ de recherche d'articles présent")

    js = (STATIC / "js" / "payment.js").read_text(encoding="utf-8")
    add = js[js.index("function addToCart(") : js.index("function renderCart()")]
    _expect("els.catalogSearch.value = ''" in add, "recherche vidée à l'ajout")
    _expect("renderCatalog();" in add, "catalogue re-affiché après l'ajout")
    _expect(
        "row.addEventListener('click', () => addToCart(a))" in js, "clic souris : ajout au panier"
    )
    _expect("addToCart(a);" in js, "même ajout pour la souris et le clavier")


def test_k_recherche_equipe_reactive():
    """Onglet Équipe : la recherche filtre la liste sans rechargement."""
    app = create_app()
    with app.app_context():
        for name in ("Zequipe Alpha", "Zequipe Beta"):
            db.session.add(
                User(
                    name=name,
                    username=name.lower().replace(" ", "."),
                    team_status="mandat",
                    team_campus="brest",
                )
            )
        db.session.commit()

    client = app.test_client()
    _login(client)
    page = client.get("/admin/equipe").get_data(as_text=True)
    _expect("data-live-filter" in page, "formulaire relié au filtrage instantané")
    _expect('id="equipe-rows"' in page and 'id="equipe-footer"' in page, "liste remplaçable")
    _expect("js/admin_filter.js" in page, "script de filtrage chargé")
    _expect('<i class="bi bi-search"></i> Rechercher' not in page, "bouton Rechercher retiré")

    res = client.get("/admin/equipe?q=Zequipe+Alpha", headers={"X-Foyz-Partiel": "1"})
    _expect(res.is_json, f"réponse partielle JSON ({res.status_code})")
    data = res.get_json()
    _expect(
        "Zequipe Alpha" in data["rows_html"] and "Zequipe Beta" not in data["rows_html"],
        "fragment filtré",
    )
    _expect("1 membre (filtrés)" in data["footer_html"], "compteur du fragment à jour")


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
