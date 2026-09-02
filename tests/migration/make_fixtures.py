"""Générateur de fixtures de migration : faux dumps réalistes Brest (MySQL) et Paris (JSON).

Usage : python make_fixtures.py <dossier_cible> [logs_brest]

Produit :
- brest_foyz_<année>.sql     : dump MySQL avec logs volumineux + tables métier
- paris_export_<année>.json  : export JSON hétérogène (etudiants/transactions/lignes)
- manifest.json              : valeurs attendues (soldes par campus, comptes)

Les soldes sont exprimés en euros décimaux côté source ; le manifest contient
aussi les totaux attendus en centimes après conversion.
"""

import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

SEED = 20260902
YEAR = 2026
ADMIN_HASH = "scrypt:32768:8:1$salt$0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
LEGACY_MD5 = "5f4dcc3b5aa765d61d8327deb882cf99"  # format non compatible -> réinitialisation


def esc(value):
    if value is None:
        return "NULL"
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return f"'{text}'"


def main(target_dir, logs_rows=5000):
    random.seed(SEED)
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- users Brest
    prenoms = ["Léo", "Camille", "Hugo", "Marie", "Antoine", "Anaïs", "Tim", "Zoé",
               "Paul", "Emma", "Nathan", "Louise", "Tom", "Jade", "Baptiste", "Manon",
               "Jean-Luc", "O'Brien", "Sarah", "Maxime", "Clara", "Yanis", "Elena",
               "Marc", "Nina", "Théo", "Alix", "Raphaël", "Inès", "Gabin", "Lucie",
               "Noé"]
    noms = ["Martin", "Rousseau", "Petit", "Le Goff", "Moreau", "Costa", "Guérin",
            "L'Écuyer", "Debise", "Lefevre", "Girard", "Bonnet", "Lambert",
            "Fontaine", "Chevalier", "Robin", "D'Souza", "Quintard", "Bernard",
            "Durand", "Marchand", "Perrot", "Sylvain", "Aubert", "Noël", "Renaud",
            "Barbier", "Clement", "Fischer", "Gauthier", "Imbert", "Joly"]

    brest_users, paris_users = [], []
    expected_brest_cents, expected_paris_cents = 0, 0
    merged_emails = {"marie.legoff@etu-ensta.fr", "paul.debise@etu-ensta.fr"}

    for i in range(30):
        prenom, nom = prenoms[i], noms[i]
        email = f"{prenom.lower().replace(' ', '')}.{nom.lower().replace(chr(39), '')}@etu-ensta.fr"
        solde = round(random.uniform(-15, 60), 2)
        if i == 3:  # fusion Paris
            email = "marie.legoff@etu-ensta.fr"
        if i == 8:
            email = "paul.debise@etu-ensta.fr"
        user = {
            "id": i + 1,
            "prenom": prenom,
            "nom": nom,
            "promotion": 2025 + (i % 4),
            "email": email,
            "pseudo": f"{prenom.lower()}.{nom.lower().replace(chr(39), '')}",
            "mdp": ADMIN_HASH if i == 0 else LEGACY_MD5,
            "solde": f"{solde:.2f}",
            "statut": "mandat" if i in (0, 1) else ("ancien" if i == 4 else None),
            "titre": "Président" if i == 0 else None,
            "blacklist": 1 if i == 14 else 0,
            "blacklist_alcool": 1 if i == 15 else 0,
            "verres_restants": i % 3,
            "date_inscription": f"2025-09-{(i % 28) + 1:02d} 12:00:00",
        }
        brest_users.append(user)
        expected_brest_cents += int(round(solde * 100))

    for i in range(22):
        prenom, nom = prenoms[i + 5], noms[i + 9]
        email = f"{prenom.lower()}.{nom.lower().replace(chr(39), '')}@etu-ensta-paris.fr"
        solde = round(random.uniform(-8, 45), 2)
        if i == 0:
            email, solde = "marie.legoff@etu-ensta.fr", -4.10   # fusion
            prenom, nom = "Marie", "Le Goff"
        if i == 1:
            email, solde = "paul.debise@etu-ensta.fr", 12.30    # fusion
            prenom, nom = "Paul", "Debise"
        paris_users.append({
            "id": i + 1,
            "nom": nom,
            "prenom": prenom,
            "email": email,
            "pseudo": f"paris.{prenom.lower()}",
            "solde": f"{solde:.2f}".replace(".", ","),
            "statut_equipe": "mandat" if i == 0 else None,
            "fonction": "Trésorière" if i == 0 else None,
        })
        expected_paris_cents += int(round(solde * 100))

    # ------------------------------------------------------ transactions Brest
    types = ["rechargement", "vente", "retrait", "transfert", "consigne", "soirée_speciale"]
    moyens = ["cb", "Lydia", "espèces", "helloasso", None]
    brest_txns, brest_lines = [], []
    base_date = datetime(YEAR, 9, 1, 18, 0, 0)
    for i in range(400):
        user = random.choice(brest_users)
        txn_type = types[i % len(types)]
        montant = round(random.uniform(-40, 30), 2)
        cancelled = 1 if i % 97 == 0 else 0
        created = base_date + timedelta(hours=i % 3000)
        txn = {
            "id": i + 1,
            "date": created.strftime("%Y-%m-%d %H:%M:%S"),
            "type": txn_type,
            "montant": f"{montant:.2f}",
            "membre_id": user["id"],
            "operateur": "Léo Martin" if i % 2 else "Camille Rousseau",
            "moyen": moyens[i % len(moyens)],
            "annule": cancelled,
            "note": f"Auto-rempli n°{i}; avec point-virgule et 'quote'" if i % 50 == 0 else None,
        }
        brest_txns.append(txn)
        if txn_type == "vente" and not cancelled:
            for j in range(random.randint(1, 3)):
                qty = random.randint(1, 4)
                pu = round(random.uniform(1, 6), 2)
                brest_lines.append({
                    "id": len(brest_lines) + 1,
                    "vente_id": txn["id"],
                    "produit": random.choice(["Kronenbourg 25cl", "Chips Paprika", "Saucisson"]),
                    "quantite": qty,
                    "prix_unitaire": f"{pu:.2f}",
                    "total": f"{qty * pu:.2f}",
                })

    paris_txns = []
    for i in range(180):
        user = random.choice(paris_users)
        montant = round(random.uniform(-25, 35), 2)
        created = base_date + timedelta(hours=i % 2000)
        paris_txns.append({
            "id": i + 1,
            "date": created.strftime("%Y-%m-%dT%H:%M:%S"),
            "type": random.choice(["rechargement", "achat", "retrait"]),
            "montant": f"{montant:.2f}",
            "email": user["email"],
            "operateur": "Sarah Bernard",
            "moyen": "carte bancaire" if i % 2 else "cash",
            "annulee": False,
        })

    # ---------------------------------------------------------------- dump SQL
    def create_table(name, columns):
        cols = ",\n  ".join(f"`{c}` {t}" for c, t in columns)
        return (f"DROP TABLE IF EXISTS `{name}`;\n"
                f"CREATE TABLE `{name}` (\n  {cols},\n  PRIMARY KEY (`id`)\n"
                f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb3;\n")

    def insert_stmt(name, columns, rows):
        values = ",\n".join(
            "(" + ",".join(esc(row[c]) for c in columns) + ")" for row in rows
        )
        return f"INSERT INTO `{name}` VALUES\n{values};\n"

    dump = []
    dump.append(f"-- MySQL dump 10.13  Distrib 8.0, for Linux (x86_64)\n--")
    dump.append("/*!40101 SET NAMES utf8mb4 */;")
    dump.append("SET NAMES utf8mb4;\n")
    # logs (volumineux, exclus)
    dump.append(create_table("logs_actions", [("id", "bigint NOT NULL"),
                                              ("date", "datetime"), ("texte", "text")]))
    payload = "x" * 120
    dump.append("INSERT INTO `logs_actions` VALUES\n" + ",\n".join(
        f"({i},'2026-08-{(i % 28) + 1:02d} 2{i % 10}:{i % 60:02d}:00','action utilisateur {i} {payload}')"
        for i in range(1, logs_rows + 1)) + ";\n")
    dump.append(create_table("connexions", [("id", "bigint NOT NULL"), ("ip", "varchar(64)"),
                                            ("user_agent", "text")]))
    dump.append("INSERT INTO `connexions` VALUES\n" + ",\n".join(
        f"({i},'10.0.{i % 255}.{i % 97}','Mozilla/5.0 agent;{payload}')" for i in range(1, 800)) + ";\n")
    dump.append(create_table("sessions", [("id", "varchar(64) NOT NULL"), ("donnees", "text")]))
    dump.append("INSERT INTO `sessions` VALUES\n" + ",\n".join(
        f"('sess{i}','{payload}')" for i in range(1, 300)) + ";\n")
    dump.append(create_table("debug_trace", [("id", "bigint NOT NULL"), ("trace", "text")]))
    dump.append("INSERT INTO `debug_trace` VALUES\n" + ",\n".join(
        f"({i},'stack line {i}; {payload}')" for i in range(1, 50)) + ";\n")
    # tables métier
    dump.append(create_table("membres", [("id", "int NOT NULL"), ("prenom", "varchar(80)"),
                                         ("nom", "varchar(80)"), ("promotion", "int"),
                                         ("email", "varchar(120)"), ("pseudo", "varchar(80)"),
                                         ("mdp", "varchar(255)"), ("solde", "decimal(10,2)"),
                                         ("statut", "varchar(20)"), ("titre", "varchar(120)"),
                                         ("blacklist", "tinyint"), ("blacklist_alcool", "tinyint"),
                                         ("verres_restants", "int"), ("date_inscription", "datetime")]))
    dump.append(insert_stmt("membres",
                            ["id", "prenom", "nom", "promotion", "email", "pseudo", "mdp",
                             "solde", "statut", "titre", "blacklist", "blacklist_alcool",
                             "verres_restants", "date_inscription"], brest_users))
    dump.append(create_table("transactions", [("id", "int NOT NULL"), ("date", "datetime"),
                                              ("type", "varchar(30)"), ("montant", "decimal(10,2)"),
                                              ("membre_id", "int"), ("operateur", "varchar(120)"),
                                              ("moyen", "varchar(20)"), ("annule", "tinyint"),
                                              ("note", "varchar(255)")]))

    # INSERT étendus par paquets de 120 lignes (comme mysqldump)
    for start in range(0, len(brest_txns), 120):
        dump.append(insert_stmt("transactions",
                                ["id", "date", "type", "montant", "membre_id", "operateur",
                                 "moyen", "annule", "note"], brest_txns[start:start + 120]))
    dump.append(create_table("vente_lignes", [("id", "int NOT NULL"), ("vente_id", "int"),
                                              ("produit", "varchar(160)"), ("quantite", "int"),
                                              ("prix_unitaire", "decimal(10,2)"),
                                              ("total", "decimal(10,2)")]))

    for start in range(0, len(brest_lines), 200):
        dump.append(insert_stmt("vente_lignes",
                                ["id", "vente_id", "produit", "quantite", "prix_unitaire",
                                 "total"], brest_lines[start:start + 200]))
    dump.append("/*!40101 SET character_set_client = @saved_client */;\n")

    brest_path = target / f"brest_foyz_{YEAR}.sql"
    brest_path.write_text("\n".join(dump), encoding="utf-8")

    # -------------------------------------------------------------- paris JSON
    paris = {
        "etudiants": paris_users,
        "transactions": paris_txns,
        "lignes": [
            {"transaction_id": 1, "produit": "Guinness 33cl", "quantite": 2,
             "prix_unitaire": "4,20", "total": "8,40"},
            {"transaction_id": 999999, "produit": "orphan", "quantite": 1,
             "prix_unitaire": "1,00", "total": "1,00"},
        ],
        "logs_navigation": [
            {"page": "/catalogue", "ip": "10.4.4.4"},  # section inconnue -> non projetée
        ],
    }
    paris_path = target / f"paris_export_{YEAR}.json"
    paris_path.write_text(json.dumps(paris, ensure_ascii=False, indent=1), encoding="utf-8")

    manifest = {
        "brest_users": len(brest_users),
        "paris_users": len(paris_users),
        "merged_emails": sorted(merged_emails),
        "source_cents": {"brest": expected_brest_cents, "paris": expected_paris_cents,
                         "total": expected_brest_cents + expected_paris_cents},
        "brest_transactions": len(brest_txns),
        "paris_transactions": len(paris_txns),
        "brest_lines": len(brest_lines),
    }
    (target / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"fixtures écrites dans {target}")
    print(f"  {brest_path.name} : {brest_path.stat().st_size // 1024} KiB")
    print(f"  {paris_path.name} : {paris_path.stat().st_size // 1024} KiB")
    print(f"  attendu : brest={expected_brest_cents} c, paris={expected_paris_cents} c")


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 5000)
