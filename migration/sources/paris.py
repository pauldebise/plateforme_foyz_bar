"""Contrat de la base Paris : export hétérogène (JSON / JSONL / CSV / SQL).

Les lignes sont chargées brutes dans staging_paris_raw (colonne JSONB) puis
projetées vers le schéma cible. La nature de chaque enregistrement est
déterminée par :
1. le nom de la table/section source (ex: "etudiants", "transactions") ;
2. à défaut, un champ discriminant du record (type / entite / kind) ;
3. à défaut, la forme des clés présentes (solde+nom -> user, montant+date -> transaction).
"""

from . import map_line_row, map_transaction_row, map_user_row

USER_TABLES = {"etudiants", "étudiants", "users", "user", "membres", "membre",
               "students", "comptes", "soldes"}
TXN_TABLES = {"transactions", "transaction", "ventes", "vente", "operations",
              "opérations", "mouvements", "rechargements"}
LINE_TABLES = {"lignes", "lines", "vente_lignes", "lignes_vente", "detail_ventes",
               "transaction_lines"}

KIND_FIELDS = ("type", "entite", "entité", "entity", "kind", "categorie", "objet")


def entity_for(source_table, record):
    """Entité logique ("users" | "transactions" | "lines") pour un record Paris."""
    name = (source_table or "").strip().lower()
    if name in USER_TABLES:
        return "users"
    if name in TXN_TABLES:
        return "transactions"
    if name in LINE_TABLES:
        return "lines"
    # champ discriminant éventuel
    for field in KIND_FIELDS:
        value = record.get(field)
        if value is None:
            continue
        v = str(value).strip().lower()
        if v in USER_TABLES or v in ("etudiant", "étudiant", "student", "user", "membre"):
            return "users"
        if v in TXN_TABLES or v in ("transaction", "vente", "operation", "opération"):
            return "transactions"
        if v in LINE_TABLES or v in ("ligne", "line"):
            return "lines"
    # inférence par la forme
    keys = {str(k).lower() for k in record}
    has_balance = keys & {"solde", "balance", "credit"}
    has_identity = keys & {"nom", "last_name", "lastname", "prenom", "prénom", "email", "pseudo"}
    has_amount = keys & {"montant", "montant_total", "total", "prix_total"}
    has_date = keys & {"date", "created_at", "date_heure", "horodatage", "date_transaction"}
    has_product = keys & {"produit", "article", "article_name", "quantite", "quantité"}
    if has_balance and has_identity:
        return "users"
    if has_amount and (has_date or has_product):
        return "transactions" if not has_product else "lines"
    if has_product:
        return "lines"
    if has_identity:
        return "users"
    return None


def map_record(source_table, record, campus, money_unit):
    """Projette un record Paris : retourne (entité | None, objet canonique | None)."""
    entity = entity_for(source_table, record)
    if entity == "users":
        user, _ = map_user_row(record, campus, money_unit)
        return entity, user
    if entity == "transactions":
        return entity, map_transaction_row(record, campus, money_unit)
    if entity == "lines":
        return entity, map_line_row(record, money_unit)
    return None, None
