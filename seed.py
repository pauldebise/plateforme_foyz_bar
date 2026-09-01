import random
from datetime import timedelta

from app import create_app
from app.extensions import db
from app.models import Article, Event, Keg, Note, Tap, UsefulLink, User
from app.services import catalog as C
from app.services import settings as S
from app.services import transactions as T
from app.utils import new_token, utcnow

app = create_app()


def eur(v):
    return int(v * 100)


def article(name, type_, std_b, std_p, team_b, team_p, volume=None, alcohol=None):
    if alcohol is None:
        alcohol = type_ in ("biere", "vin", "cidre")
    return Article(
        name=name, article_type=type_, volume_cl=volume, is_alcohol=alcohol,
        price_std_brest=eur(std_b), price_std_paris=eur(std_p),
        price_team_brest=eur(team_b), price_team_paris=eur(team_p),
    )


def user(first, last, promo, **kw):
    u = User(first_name=first, last_name=last, promotion=promo, **kw)
    db.session.add(u)
    db.session.flush()
    u.wallet("brest")
    u.wallet("paris")
    return u


with app.app_context():
    print("Seed des données…")

    S.set_setting("link_repository", "https://github.com/ensta/foyz-plateforme")
    S.set_setting("link_hosting", "https://hebergement.exemple.fr")
    S.set_setting("link_database", "https://bdd.exemple.fr")
    S.set_admin_password(app.config.get("DEFAULT_ADMIN_PASSWORD", "admin"))

    equipe = {
        "president_b": user("Léo", "Martin", 2027, team_status="mandat", team_campus="brest", team_title="Président", username="leo.martin", blacklist_alcohol=False),
        "barman_b1": user("Camille", "Rousseau", 2028, team_status="mandat", team_campus="brest", team_title="Maître barman", username="camille.rousseau"),
        "barman_b2": user("Hugo", "Petit", 2027, team_status="mandat", team_campus="brest", username="hugo.petit"),
        "president_p": user("Sarah", "Bernard", 2027, team_status="mandat", team_campus="paris", team_title="Présidente", username="sarah.bernard"),
        "barman_p1": user("Maxime", "Durand", 2028, team_status="mandat", team_campus="paris", username="maxime.durand"),
        "ancien_b": user("Antoine", "Moreau", 2026, team_status="ancien", team_campus="brest", username="antoine.moreau"),
    }
    from werkzeug.security import generate_password_hash

    for u in equipe.values():
        u.password_hash = generate_password_hash("foyz2026")
    equipe["barman_b2"].blacklist_alcohol = True

    etudiants = [
        user("Paul", "Debise", 2028),
        user("Emma", "Lefevre", 2027),
        user("Nathan", "Girard", 2029),
        user("Louise", "Bonnet", 2028),
        user("Tom", "Lambert", 2027),
        user("Jade", "Fontaine", 2029),
        user("Baptiste", "Chevalier", 2028),
        user("Manon", "Robin", 2029),
    ]
    etudiants[6].blacklist = True
    etudiants[7].blacklist_alcohol = True

    arts = [
        article("Kronenbourg 25cl", "biere", 1.8, 2.0, 1.4, 1.5, 25),
        article("Bretagne IPA 33cl", "biere", 3.5, 3.8, 2.8, 3.0, 33),
        article("Guinness 33cl", "biere", 4.0, 4.2, 3.2, 3.4, 33),
        article("Château Plantier Rouge — Bouteille", "vin", 8.0, 9.0, 6.5, 7.5, 75),
        article("Blanc Sec — Bouteille", "vin", 7.5, 8.5, 6.0, 7.0, 75),
        article("Cidre Fermier Breton 33cl", "cidre", 3.0, 3.5, 2.4, 2.8, 33),
        article("Chips Paprika", "snack", 1.0, 1.2, 0.8, 1.0),
        article("Kit-Kat", "snack", 1.2, 1.2, 1.0, 1.0),
        article("Saucisson Sec 100g", "saucisson", 4.5, 5.0, 3.8, 4.2),
        article("Saucisson Fromager", "saucisson", 5.0, 5.5, 4.2, 4.6),
    ]
    db.session.add_all(arts)

    futs = [
        Keg(name="Kelt Blonde", alcohol_degree=4.7, volume_l=30, remaining_l=30),
        Keg(name="Morgat Ambrée", alcohol_degree=6.2, volume_l=30, remaining_l=22.4),
        Keg(name="Sainte-Gwendoline", alcohol_degree=4.5, volume_l=20, remaining_l=20),
    ]
    db.session.add_all(futs)
    db.session.flush()
    for k, (h, p, pot) in zip(futs, [(2.5, 4.5, 3.5), (3.0, 5.5, 4.0), (2.0, 4.0, 3.0)]):
        for c in ("brest", "paris"):
            row = k.price_row(c)
            row.price_half_std, row.price_pint_std, row.price_pot_std = eur(h), eur(p), eur(pot)
            row.price_half_team, row.price_pint_team, row.price_pot_team = eur(h - 0.5), eur(p - 1), eur(pot - 0.7)

    taps_b = [Tap(number=1, campus="brest"), Tap(number=2, campus="brest")]
    taps_p = [Tap(number=3, campus="paris")]
    db.session.add_all(taps_b + taps_p)
    db.session.flush()
    C.assign_keg(taps_b[0], futs[0])
    C.assign_keg(taps_b[1], futs[1])
    C.assign_keg(taps_p[0], futs[2])

    now = utcnow()
    ev = Event(
        name="Soirée Intégration BDE",
        campus="brest",
        starts_at=now - timedelta(hours=2),
        ends_at=now + timedelta(hours=6),
        token=new_token(),
    )
    db.session.add(ev)
    db.session.flush()
    db.session.add(Article(
        name="Coupe Champagne Event", article_type="evenement", volume_cl=12,
        is_alcohol=True, event_id=ev.id,
        price_std_brest=eur(5), price_std_paris=eur(5),
        price_team_brest=eur(4), price_team_paris=eur(4),
    ))

    db.session.add_all([
        Note(content="Le Foy'z ouvre à 18h ce vendredi ! Venez nombreux 🍻", is_public=True, author_name="Léo Martin"),
        Note(content="Nouveautés cidre breton disponible au comptoir.", is_public=True, author_name="Camille Rousseau"),
        Note(content="Pensez à rendre vos verres consignés, la caisse tourne.", is_public=False, author_name="Hugo Petit"),
        Note(content="Réunion mandat mardi 19h en salle Foy'z.", is_public=False, author_name="Léo Martin"),
    ])
    db.session.add_all([
        UsefulLink(label="Plateforme de signalement VSS — Brest", url="https://www.ensta-bretagne.fr/vss", position=0),
        UsefulLink(label="Plateforme de signalement VSS — Paris", url="https://www.ensta-paris.fr/vss", position=1),
        UsefulLink(label="Site du BDE", url="https://bde.ensta.fr", position=2),
    ])

    S.set_setting("max_postits_private", "20")
    S.set_setting("max_postits_public", "10")
    db.session.commit()

    leo = equipe["president_b"]
    paul, emma, nathan = etudiants[0], etudiants[1], etudiants[2]

    T.create_reload(operator_label="Seed", campus="brest", user=paul, amount_cents=eur(30), payment_method="cb")
    T.create_reload(operator_label="Seed", campus="brest", user=emma, amount_cents=eur(20), payment_method="lydia")
    T.create_reload(operator_label="Seed", campus="brest", user=nathan, amount_cents=eur(25), payment_method="especes")
    T.create_reload(operator_label="Seed", campus="paris", user=etudiants[3], amount_cents=eur(35), payment_method="helloasso")

    T.create_purchase(
        operator_label="Léo Martin", campus="brest",
        items=[{"article_id": arts[0].id, "quantity": 2}, {"article_id": arts[6].id, "quantity": 1}],
        contributor_ids=[paul.id],
    )
    T.create_purchase(
        operator_label="Camille Rousseau", campus="brest",
        items=[{"article_id": arts[1].id, "quantity": 1}, {"article_id": arts[8].id, "quantity": 1}],
        contributor_ids=[paul.id, emma.id],
    )
    T.create_purchase(
        operator_label="Léo Martin", campus="brest",
        items=[{"article_id": arts[2].id, "quantity": 2}],
        contributor_ids=[nathan.id], deposit_glasses=2,
    )
    T.create_purchase(operator_label="Sarah Bernard", campus="paris", items=[{"article_id": arts[4].id, "quantity": 1}], contributor_ids=[etudiants[3].id])
    T.create_purchase(operator_label="Seed", campus="brest", items=[{"article_id": arts[5].id, "quantity": 2}], direct=True, payment_method="especes")

    T.create_transfer(operator_label="Seed", campus="brest", from_user=emma, to_user=nathan, amount_cents=eur(5))
    T.create_withdrawal(operator_label="Seed", campus="brest", user=nathan, amount_cents=eur(10))
    T.return_glasses(operator_label="Seed", campus="brest", user=nathan, count=1)

    treso_seed = [
        ("rechargement", 6, 120), ("rechargement", 5, 90), ("rechargement", 4, 140),
        ("rechargement", 3, 80), ("rechargement", 2, 110),
    ]
    for _type, months_ago, amount in treso_seed:
        t = T.create_reload(operator_label="Seed", campus="brest", user=paul, amount_cents=eur(amount), payment_method=random.choice(["cb", "lydia", "especes", "helloasso"]))
        t.created_at = utcnow() - timedelta(days=30 * months_ago)
    db.session.commit()

    print("Seed terminé : comptes, articles, fûts, tireuses, événement, notes, liens et transactions de démonstration.")
    print("Comptes équipe : leo.martin / camille.rousseau / hugo.petit / sarah.bernard / maxime.durand / antoine.moreau — mot de passe : foyz2026")
