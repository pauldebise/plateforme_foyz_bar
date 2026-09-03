"""Contrats de mapping des sources : alias de colonnes et convertisseurs.

Les anciennes bases ont des nommages hétérogènes (français/anglais). Chaque
entité métier (users, transactions, lines) est décrite par un dictionnaire
d'alias : la première colonne présente dans la ligne fournit la valeur.
Les contrats sont centralisés ici pour être ajustables la veille de la bascule
sans toucher au moteur ETL.
"""

from ..util import normalize_key, parse_dt, to_cents

# ---------------------------------------------------------------- alias utils


def pick(row, aliases):
    """Première valeur non vide parmi les alias (insensible à la casse)."""
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in aliases:
        value = lower.get(alias.lower())
        if value is not None and value != "":
            return value
    return None


def as_bool(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "vrai", "oui", "y", "o", "yes", "t"):
        return True
    if text in ("0", "false", "faux", "non", "n", "no", "f"):
        return False
    return None


def as_int(value):
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


# ------------------------------------------------- vocabulaires normalisés

# Types de transactions cibles (app.utils.TRANSACTION_TYPES) :
# achat | direct | rechargement | retrait | transfert | consigne
TYPE_MAP = {
    "achat": "achat", "achats": "achat", "vente": "achat", "ventes": "achat",
    "achat_comptoir": "achat",
    "rechargement": "rechargement", "recharge": "rechargement",
    "rechargements": "rechargement", "credit": "rechargement", "crédit": "rechargement",
    "depot": "rechargement", "dépôt": "rechargement",
    "retrait": "retrait", "withdrawal": "retrait", "remboursement": "retrait",
    "transfert": "transfert", "virement": "transfert", "transfer": "transfert",
    "consigne": "consigne", "retour_consigne": "consigne", "retour de consigne": "consigne",
    "direct": "direct", "paiement_direct": "direct",
}

# Moyens de paiement cibles (app.utils.PAYMENT_METHODS) :
# cb | lydia | especes | helloasso
PAYMENT_MAP = {
    "cb": "cb", "carte": "cb", "carte bancaire": "cb", "carte_bleue": "cb",
    "cb bancaire": "cb", "credit card": "cb", "visa": "cb", "mastercard": "cb",
    "lydia": "lydia", "lydia-commun": "lydia",
    "especes": "especes", "espèces": "especes", "espece": "especes",
    "espèce": "especes", "cash": "especes", "monnaie": "especes",
    "helloasso": "helloasso", "hello asso": "helloasso",
}

TEAM_STATUS_MAP = {
    "mandat": "mandat", "membre": "mandat", "membre_equipe": "mandat",
    "membre équipe": "mandat", "equipe": "mandat", "équipe": "mandat",
    "bureau": "mandat", "admin": "mandat", "president": "mandat",
    "ancien": "ancien", "ancien membre": "ancien", "alumni": "ancien",
    "anciens": "ancien",
    # Brest : users.is_foyz (tinyint 0/1) -> statut équipe
    "1": "mandat",
}


def normalize_type(value):
    if value is None:
        return None
    return TYPE_MAP.get(str(value).strip().lower())


def normalize_payment(value):
    if value is None or str(value).strip() == "":
        return None
    return PAYMENT_MAP.get(str(value).strip().lower())


def normalize_team_status(value):
    if value is None:
        return None
    return TEAM_STATUS_MAP.get(str(value).strip().lower())


# ------------------------------------------------------------ alias tables

USER_FIELDS = {
    "src_id": ("id", "membre_id", "student_id", "utilisateur_id", "user_id", "id_etudiant",
               "card_id"),
    "first_name": ("prenom", "prénom", "first_name", "firstname", "real_name"),
    "last_name": ("nom", "last_name", "lastname", "famille"),
    "promotion": ("promotion", "promo", "annee", "année", "year", "promotion_annee"),
    "email": ("email", "mail", "courriel", "adresse_mail"),
    "username": ("pseudo", "username", "login", "identifiant", "name"),
    "password": ("mdp", "password", "password_hash", "mot_de_passe", "pass", "hash"),
    "balance": ("solde", "balance", "solde_euros", "credit", "solde_compte"),
    "team_status": ("statut", "statut_equipe", "team_status", "role_equipe", "status",
                    "is_foyz"),
    "team_title": ("titre", "team_title", "fonction", "poste"),
    "team_campus": ("campus_equipe", "team_campus", "campus"),
    "photo": ("photo", "avatar", "photo_url"),
    "blacklist": ("blacklist", "blacklisted", "interdit", "liste_noire"),
    "blacklist_alcohol": ("blacklist_alcool", "blacklist_alcohol", "sans_alcool",
                          "alcohol_blacklisted"),
    "glasses_outstanding": ("verres_restants", "verres", "glasses_outstanding",
                            "consignes_restantes", "verres_sortis", "ecocups"),
    "created_at": ("date_inscription", "created_at", "date_creation", "inscription", "cree_le",
                   "registration"),
}

TXN_FIELDS = {
    "src_id": ("id", "transaction_id", "vente_id", "operation_id", "id_vente"),
    "created_at": ("date", "date_heure", "date_transaction", "created_at", "horodatage",
                   "date_vente", "timestamp"),
    "type": ("type", "type_transaction", "categorie", "catégorie", "nature"),
    "total": ("montant", "montant_total", "total", "prix_total", "valeur", "somme",
              "balance"),
    "user_id": ("membre_id", "user_id", "etudiant_id", "compte_id", "client_id"),
    "operator": ("operateur", "opérateur", "operator", "caissier", "vendeur",
                 "operateur_label", "logged_user_id"),
    "payment_method": ("moyen", "moyen_paiement", "payment_method", "paiement", "reglement"),
    "cancelled": ("annule", "annulée", "annulee", "cancelled", "est_annule", "annulation"),
    "cancelled_at": ("date_annulation", "cancelled_at", "annule_le"),
    "note": ("note", "commentaire", "remarque", "motif"),
    "deposit_glasses": ("verres_consignes", "deposit_glasses", "nb_verres", "verres"),
}

LINE_FIELDS = {
    "transaction_id": ("transaction_id", "vente_id", "id_vente", "id_transaction",
                       "operation_id"),
    "article_name": ("produit", "article", "article_name", "nom_produit", "designation",
                     "désignation", "nom"),
    "quantity": ("quantite", "quantité", "quantity", "qte", "qté"),
    "unit_price": ("prix_unitaire", "unit_price", "pu", "prix", "article_price"),
    "line_total": ("total", "montant", "line_total", "montant_ligne", "prix_total", "sous_total"),
    "article_type": ("type_article", "article_type", "categorie", "catégorie", "famille"),
}

ARTICLE_TYPES_KNOWN = {"biere", "vin", "cidre", "snack", "saucisson", "evenement"}


# ---------------------------------------------------------------- mappers

def map_user_row(row, campus, money_unit):
    """Convertit une ligne source en compte canonique (montants en centimes).

    Retourne (dict | None, warning | None). None = ligne ignorée (identité absente).
    """
    first = pick(row, USER_FIELDS["first_name"]) or ""
    last = pick(row, USER_FIELDS["last_name"]) or ""
    email = pick(row, USER_FIELDS["email"])
    username = pick(row, USER_FIELDS["username"])
    if not (first or last) and not (email or username):
        return None, "identité absente (ni nom, ni email/pseudo)"
    key = normalize_key(email) or normalize_key(username) or normalize_key(f"{first}.{last}")
    if not key:
        return None, "clé de réconciliation vide"
    if not (first or last):
        # identité reconstituée depuis l'identifiant pour l'affichage minimal
        first = username or email or "?"
    password = pick(row, USER_FIELDS["password"])
    from ..util import looks_like_werkzeug_hash
    hash_val = str(password) if looks_like_werkzeug_hash(password) else None
    balance = to_cents(pick(row, USER_FIELDS["balance"]), money_unit,
                       context=f"solde {campus}")
    team_campus = pick(row, USER_FIELDS["team_campus"])
    team_campus = team_campus.lower() if str(team_campus or "").lower() in ("brest", "paris") else None
    user = {
        "src_id": pick(row, USER_FIELDS["src_id"]),
        "key": key,
        "email": email,
        "first_name": str(first).strip(),
        "last_name": str(last).strip(),
        "promotion": as_int(pick(row, USER_FIELDS["promotion"])),
        "username": (str(username).strip() or None) if username else None,
        "password_hash": hash_val,
        "team_status": normalize_team_status(pick(row, USER_FIELDS["team_status"])),
        "team_title": pick(row, USER_FIELDS["team_title"]),
        "team_campus": team_campus,
        "photo": pick(row, USER_FIELDS["photo"]),
        "blacklist": as_bool(pick(row, USER_FIELDS["blacklist"])) or False,
        "blacklist_alcohol": as_bool(pick(row, USER_FIELDS["blacklist_alcohol"])) or False,
        "glasses_outstanding": as_int(pick(row, USER_FIELDS["glasses_outstanding"])) or 0,
        "created_at": parse_dt(pick(row, USER_FIELDS["created_at"])),
        "balance_cents": balance,
        "campus": campus,
    }
    return user, None


def map_transaction_row(row, campus, money_unit):
    """Convertit une ligne source en transaction canonique (centimes)."""
    total_raw = pick(row, TXN_FIELDS["total"])
    total = to_cents(total_raw, money_unit, context=f"transaction {campus}")
    cancelled = as_bool(pick(row, TXN_FIELDS["cancelled"])) or False
    legacy_type = pick(row, TXN_FIELDS["type"])
    txn_type = normalize_type(legacy_type)
    payment = normalize_payment(pick(row, TXN_FIELDS["payment_method"]))
    article_note = None
    if legacy_type is not None and txn_type is None:
        article_note = f"type source inconnu : {legacy_type!r}"
    return {
        "src_id": pick(row, TXN_FIELDS["src_id"]),
        "created_at": parse_dt(pick(row, TXN_FIELDS["created_at"])),
        "type": txn_type,
        "total_cents": total,
        "src_user_id": pick(row, TXN_FIELDS["user_id"]),
        "operator_label": str(pick(row, TXN_FIELDS["operator"]) or "")[:120],
        "payment_method": payment,
        "cancelled": cancelled,
        "cancelled_at": parse_dt(pick(row, TXN_FIELDS["cancelled_at"])),
        "note": pick(row, TXN_FIELDS["note"]),
        "deposit_glasses": as_int(pick(row, TXN_FIELDS["deposit_glasses"])) or 0,
        "campus": campus,
        "type_warning": article_note,
    }


def map_line_row(row, money_unit):
    """Convertit une ligne de détail de vente (article_name, qté, prix)."""
    name = pick(row, LINE_FIELDS["article_name"])
    if name is None and pick(row, LINE_FIELDS["transaction_id"]) is None:
        return None
    article_type = pick(row, LINE_FIELDS["article_type"])
    article_type = str(article_type).strip().lower() if article_type else None
    if article_type not in ARTICLE_TYPES_KNOWN:
        article_type = "biere"
    quantity = as_int(pick(row, LINE_FIELDS["quantity"])) or 1
    unit_price = to_cents(pick(row, LINE_FIELDS["unit_price"]), money_unit,
                          context="ligne (prix unitaire)")
    line_total = to_cents(pick(row, LINE_FIELDS["line_total"]), money_unit,
                          context="ligne (total)")
    if not line_total and unit_price:
        line_total = unit_price * quantity
    return {
        "src_transaction_id": pick(row, LINE_FIELDS["transaction_id"]),
        "article_name": str(name or "?")[:200],
        "article_type": article_type,
        "quantity": quantity,
        "unit_price": unit_price,
        "line_total": line_total,
    }
