"""Contrat de la base Brest : dump MySQL classique (mysqldump).

Liste blanche des tables métier migrées (les tables de logs, volumineuses,
sont exclues à la volée pendant le streaming — cf. logfilter.py) :

    users         -> membres / étudiants (solde en euros décimaux)
    transactions  -> historique comptable conservé (justifie les soldes)
    lines         -> lignes de détail des ventes (table `baskets`)

Schéma réel Brest (dump du 31/08) : users(card_id, name, real_name, password,
balance, blacklisted, is_foyz, promo, disabled, registration, ecocups,
alcohol_blacklisted), transactions(id, date, user_id, balance=montant,
logged_user_id), baskets(id, transaction_id, article_id, article_name,
article_price, article_type, article_volume, quantity, beer_draught,
extra_type). Aucun email : la clé de réconciliation est le pseudo (`name`).

Ajuster TABLE_MAP / USER_FIELDS / TXN_FIELDS (dans sources/__init__.py) si le
schéma réel diffère la veille de la bascule.
"""

from . import USER_FIELDS, TXN_FIELDS, LINE_FIELDS  # noqa: F401 (contrat centralisé)

# table source (minuscules) -> entité logique. Toute table absente de cette
# liste n'est PAS migrée : si son nom ressemble à un log elle est classée
# "log", sinon "inconnue" (rapport d'audit).
TABLE_MAP = {
    "membres": "users",
    "users": "users",
    "etudiants": "users",
    "étudiants": "users",
    "students": "users",
    "utilisateurs": "users",
    "comptes": "users",
    "transactions": "transactions",
    "ventes": "transactions",
    "operations": "transactions",
    "opérations": "transactions",
    "achats": "transactions",
    "rechargements": "transactions",
    "mouvements": "transactions",
    "transaction_lines": "lines",
    "vente_lignes": "lines",
    "lignes_vente": "lines",
    "detail_ventes": "lines",
    "détail_ventes": "lines",
    "lignes_transaction": "lines",
    "baskets": "lines",
}

ENTITY_FIELDS = {"users": USER_FIELDS, "transactions": TXN_FIELDS, "lines": LINE_FIELDS}


def entity_for(table_name):
    """Entité logique migrée pour une table source, None si hors liste blanche."""
    if not table_name:
        return None
    return TABLE_MAP.get(table_name.strip().lower())
