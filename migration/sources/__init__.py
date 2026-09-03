"""Contrats de mapping des sources : alias de colonnes et convertisseurs.

Les anciennes bases ont des nommages hétérogènes (français/anglais). Chaque
entité métier (users, transactions, lines, articles, kegs, taps) est décrite
par un dictionnaire d'alias : la première colonne présente dans la ligne
fournit la valeur. Les contrats sont centralisés ici pour être ajustables la
veille de la bascule sans toucher au moteur ETL.
"""

import re
import unicodedata

from ..util import liters_to_cl, normalize_key, parse_dt, to_cents, unescape_html

# ---------------------------------------------------------------- alias utils


def pick(row, aliases):
    """Première valeur non vide parmi les alias (insensible à la casse)."""
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in aliases:
        value = lower.get(alias.lower())
        if value is not None and value != "":
            return value
    return None


# Littéral MySQL bit(1) produit par mysqldump : b'0', b'1', b'\0', b'\x01'
_BIT_RE = re.compile(r"^b'(\\x[0-9a-f]{1,2}|\\0|[01])'$", re.IGNORECASE)


def as_bool(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    bit = _BIT_RE.match(text)
    if bit:
        content = bit.group(1).lower()
        return content not in ("0", "\\0", "\\x00")
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

# Identité : un seul champ cible `name` (nom / surnom, identifiant de connexion).
NAME_FIELDS = ("name", "pseudo", "username", "login", "identifiant", "nom_surnom",
               "real_name")
# Certaines sources séparent prénom / nom : combinés si aucun champ `name`.
FIRST_NAME_FIELDS = ("prenom", "prénom", "first_name", "firstname")
LAST_NAME_FIELDS = ("nom", "last_name", "lastname", "famille")

USER_FIELDS = {
    "src_id": ("id", "membre_id", "student_id", "utilisateur_id", "user_id", "id_etudiant",
               "card_id"),
    "email": ("email", "mail", "courriel", "adresse_mail"),
    "password": ("mdp", "password", "password_hash", "mot_de_passe", "pass", "hash"),
    "balance": ("solde", "balance", "solde_euros", "credit", "solde_compte"),
    "team_status": ("statut", "statut_equipe", "team_status", "role_equipe", "status",
                    "is_foyz"),
    "team_campus": ("campus_equipe", "team_campus", "campus"),
    "blacklist": ("blacklist", "blacklisted", "interdit", "liste_noire"),
    "blacklist_alcohol": ("blacklist_alcool", "blacklist_alcohol", "sans_alcool",
                          "alcohol_blacklisted"),
    "blacklist_reason": ("blacklist_reason", "motif_blacklist", "motif_blacklisting",
                         "raison_blacklist", "raison_interdiction", "motif_interdiction"),
    "glasses_outstanding": ("verres_restants", "verres", "glasses_outstanding",
                            "consignes_restantes", "verres_sortis", "ecocups"),
    "created_at": ("date_inscription", "created_at", "date_creation", "inscription", "cree_le",
                   "registration"),
    "promotion": ("promotion", "promo", "annee", "année", "year", "promotion_annee"),
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
    "article_id": ("article_id", "id_article", "produit_id", "code_barre", "barcode"),
    "article_name": ("produit", "article", "article_name", "nom_produit", "designation",
                     "désignation", "nom"),
    "quantity": ("quantite", "quantité", "quantity", "qte", "qté"),
    "unit_price": ("prix_unitaire", "unit_price", "pu", "prix", "article_price"),
    "line_total": ("total", "montant", "line_total", "montant_ligne", "prix_total", "sous_total"),
    "article_type": ("type_article", "article_type", "categorie", "catégorie", "famille"),
}

# Catalogue : le prix `std` est le prix public, `team` le prix membre (Brest :
# `price` / `price_foyz`). Le volume source Brest est exprimé en litres.
ARTICLE_FIELDS = {
    "src_id": ("id", "article_id", "id_article", "code", "code_barre", "barcode", "ref"),
    "name": ("name", "nom", "article", "produit", "designation", "désignation",
             "libelle", "libellé"),
    "type": ("type", "type_article", "article_type", "categorie", "catégorie", "famille"),
    "volume_l": ("volume", "volume_l"),
    "volume_cl": ("volume_cl", "contenance", "contenance_cl"),
    "price_std": ("price", "prix", "price_std", "prix_std", "prix_vente"),
    "price_team": ("price_foyz", "prix_foyz", "price_team", "prix_equipe", "prix_équipe",
                   "prix_membre"),
    "active": ("active", "actif", "visible", "disponible"),
}

# Fûts (Brest `draft_beers`) : tarifs par format (demi/pinte/pot) x public/équipe.
KEG_FIELDS = {
    "src_id": ("id", "keg_id", "fut_id", "draft_beer_id"),
    "name": ("name", "nom", "beer_name", "biere", "bière"),
    "volume_l": ("volume", "volume_l", "contenance_l"),
    "alcohol_degree": ("alcohol_volume", "alcohol_degree", "degre", "degré", "degres",
                       "degrés", "alcool"),
    "price_half_std": ("half_pint_price", "prix_demi", "price_half"),
    "price_half_team": ("half_pint_price_foyz", "prix_demi_foyz", "price_half_team"),
    "price_pint_std": ("pint_price", "prix_pinte", "price_pint"),
    "price_pint_team": ("pint_price_foyz", "prix_pinte_foyz", "price_pint_team"),
    "price_pot_std": ("pot_price", "prix_pot", "price_pot"),
    "price_pot_team": ("pot_price_foyz", "prix_pot_foyz", "price_pot_team"),
}

# Tireuses (Brest `draft_beer_current`) : seul l'état courant (date_end NULL)
# configure la cible ; l'historique d'occupation est ignoré.
TAP_FIELDS = {
    "src_id": ("id", "tap_id"),
    "draught": ("beer_draught", "draught", "tireuse", "tap"),
    "keg_id": ("draft_beer_id", "keg_id", "fut_id"),
    "date_start": ("date_start", "date_debut", "depuis"),
    "date_end": ("date_end", "date_fin"),
}

# Types d'articles Brest (table `article_types`) -> vocabulaire cible
# (app.utils.ARTICLE_TYPES : biere|vin|cidre|snack|saucisson|evenement).
# « Boisson Chaude » / « Boisson Froide » n'existent pas en cible : rattachés
# aux consommables non alcoolisés (`snack`). Ajustable la veille de la bascule.
ARTICLE_TYPE_MAP = {
    "bière": "biere", "biere": "biere",
    "vin": "vin",
    "cidre": "cidre",
    "snacks": "snack", "snack": "snack",
    "saucisson": "saucisson",
    "boisson chaude": "snack",
    "boisson froide": "snack",
    "cocktails/barbecue/soirées": "evenement",
    "cocktails/barbecue/soirees": "evenement",
    "cocktail": "evenement", "cocktails": "evenement",
    "soirée": "evenement", "soiree": "evenement",
    "evenement": "evenement", "évènement": "evenement", "événement": "evenement",
}
# Type par défaut d'un article de type source inconnu : consommable NON
# alcoolisé (jamais « biere » : le type pilote le contrôle blacklist alcool).
DEFAULT_ARTICLE_TYPE = "snack"

ARTICLE_TYPES_KNOWN = {"biere", "vin", "cidre", "snack", "saucisson", "evenement"}
ALCOHOLIC_ARTICLE_TYPES = {"biere", "vin", "cidre"}

# Tireuses : nom Brest (enum `draft_beer_current.beer_draught`) -> numéro cible
# (taps.number, unique toutes campus confondues).
TAP_NUMBERS = {
    "tireuse de gauche": 1,
    "tireuse de droite": 2,
    "tireuse du milieu": 3,
    "tireuse mobile": 4,
}
# Formats vendus au comptoir sur une tireuse — copie de app.utils.TAP_SIZES
# (le module migration reste volontairement sans dépendance Flask) ; les
# articles de tireuse sont régénérés à l'identique de app.services.catalog.
TAP_SIZES = {"demi": ("Demi", 25), "pinte": ("Pinte", 50), "pot": ("Pot", 33)}


# ---------------------------------------------------------------- mappers

def map_user_row(row, campus, money_unit):
    """Convertit une ligne source en compte canonique (montants en centimes).

    Retourne (dict | None, warning | None). None = ligne ignorée (identité absente).
    L'identité cible est un seul champ `name` (nom / surnom) ; les sources qui
    séparent prénom / nom sont combinées. La clé de réconciliation reste
    l'email quand il existe (fusion inter-campus), sinon le nom.
    """
    name = pick(row, NAME_FIELDS)
    if not name:
        first = pick(row, FIRST_NAME_FIELDS) or ""
        last = pick(row, LAST_NAME_FIELDS) or ""
        name = f"{first} {last}".strip()
    email = pick(row, USER_FIELDS["email"])
    if not name and not email:
        return None, "identité absente (ni nom, ni email)"
    name = str(name).strip() if name is not None else ""
    if not name:
        name = str(email)
    key = normalize_key(email) or normalize_key(name)
    if not key:
        return None, "clé de réconciliation vide"
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
        "name": name[:80],
        "promotion": as_int(pick(row, USER_FIELDS["promotion"])),
        "password_hash": hash_val,
        "team_status": normalize_team_status(pick(row, USER_FIELDS["team_status"])),
        "team_campus": team_campus,
        "blacklist": as_bool(pick(row, USER_FIELDS["blacklist"])) or False,
        "blacklist_alcohol": as_bool(pick(row, USER_FIELDS["blacklist_alcohol"])) or False,
        "blacklist_reason": _clean_reason(pick(row, USER_FIELDS["blacklist_reason"])),
        "glasses_outstanding": as_int(pick(row, USER_FIELDS["glasses_outstanding"])) or 0,
        "created_at": parse_dt(pick(row, USER_FIELDS["created_at"])),
        "balance_cents": balance,
        "campus": campus,
    }
    return user, None


def _clean_reason(value):
    """Motif de blacklist : texte brut borné à 255 caractères (None si vide)."""
    if value is None:
        return None
    text = unescape_html(str(value)).strip()
    return text[:255] or None


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


def resolve_article_type(raw, type_names=None):
    """Type source -> type cible.

    `raw` peut être un id de la table Brest `article_types` (ex : 6), un nom de
    type ('Bière') ou déjà un type cible ('biere'). `type_names` : {id: nom}
    résolu en amont depuis cette table de référence. Inconnu -> DEFAULT_ARTICLE_TYPE.
    """
    if raw is None or str(raw).strip() == "":
        return DEFAULT_ARTICLE_TYPE
    value = str(raw).strip()
    if as_int(value) is not None and type_names is not None:
        value = str(type_names.get(int(value)) or raw)
    target = ARTICLE_TYPE_MAP.get(value.strip().lower())
    if target is None:
        target = ARTICLE_TYPE_MAP.get(unnormalize(value))
    return target or DEFAULT_ARTICLE_TYPE


def unnormalize(value):
    """'Biére' -> 'biere' : garde-fou NFKD pour les variantes d'accents."""
    text = unicodedata.normalize("NFKD", str(value).strip().lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def map_line_row(row, money_unit, type_names=None):
    """Convertit une ligne de détail de vente (article_name, qté, prix).

    Brest `baskets` porte le type d'article sous forme d'id (`article_types`) :
    il est résolu via `type_names` ({id: nom}, préchargé depuis cette table de
    référence). Sans aucun type source, fallback historique : "biere".
    """
    name = pick(row, LINE_FIELDS["article_name"])
    if name is None and pick(row, LINE_FIELDS["transaction_id"]) is None:
        return None
    raw_type = pick(row, LINE_FIELDS["article_type"])
    if raw_type is None:
        article_type = "biere"
    else:
        article_type = resolve_article_type(raw_type, type_names)
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
        "src_article_id": pick(row, LINE_FIELDS["article_id"]),
        "article_name": str(unescape_html(name) or "?")[:200],
        "article_type": article_type,
        "quantity": quantity,
        "unit_price": unit_price,
        "line_total": line_total,
    }


def map_article_row(row, money_unit, type_names=None):
    """Convertit une ligne de catalogue source en article canonique (centimes)."""
    name = pick(row, ARTICLE_FIELDS["name"])
    if name is None or not str(name).strip():
        return None
    volume_cl = as_int(pick(row, ARTICLE_FIELDS["volume_cl"]))
    if volume_cl is None:
        volume_cl = liters_to_cl(pick(row, ARTICLE_FIELDS["volume_l"]))
    price_std = to_cents(pick(row, ARTICLE_FIELDS["price_std"]), money_unit,
                         context="article (prix public)")
    price_team = to_cents(pick(row, ARTICLE_FIELDS["price_team"]), money_unit,
                          context="article (prix équipe)")
    article_type = resolve_article_type(pick(row, ARTICLE_FIELDS["type"]), type_names)
    active = as_bool(pick(row, ARTICLE_FIELDS["active"]))
    return {
        "src_id": pick(row, ARTICLE_FIELDS["src_id"]),
        "name": str(unescape_html(name)).strip()[:160],
        "article_type": article_type,
        "is_alcohol": article_type in ALCOHOLIC_ARTICLE_TYPES,
        "volume_cl": volume_cl,
        "price_std_cents": price_std,
        "price_team_cents": price_team,
        "active": True if active is None else active,
    }


def map_keg_row(row, money_unit):
    """Convertit un fût source (Brest `draft_beers`) en keg canonique + tarifs."""
    name = pick(row, KEG_FIELDS["name"])
    if name is None or not str(name).strip():
        return None

    def _deg(value):
        if value is None or value == "":
            return 0.0
        try:
            return float(str(value).strip().replace(",", "."))
        except ValueError:
            return 0.0

    volume_l = liters_to_cl(pick(row, KEG_FIELDS["volume_l"]))
    volume_l = (volume_l or 0) / 100.0
    return {
        "src_id": pick(row, KEG_FIELDS["src_id"]),
        "name": str(unescape_html(name)).strip()[:160],
        "volume_l": volume_l,
        "remaining_l": volume_l,
        "alcohol_degree": _deg(pick(row, KEG_FIELDS["alcohol_degree"])),
        "price_half_std": to_cents(pick(row, KEG_FIELDS["price_half_std"]), money_unit,
                                   context="fût (prix demi)"),
        "price_half_team": to_cents(pick(row, KEG_FIELDS["price_half_team"]), money_unit,
                                    context="fût (prix demi équipe)"),
        "price_pint_std": to_cents(pick(row, KEG_FIELDS["price_pint_std"]), money_unit,
                                   context="fût (prix pinte)"),
        "price_pint_team": to_cents(pick(row, KEG_FIELDS["price_pint_team"]), money_unit,
                                    context="fût (prix pinte équipe)"),
        "price_pot_std": to_cents(pick(row, KEG_FIELDS["price_pot_std"]), money_unit,
                                  context="fût (prix pot)"),
        "price_pot_team": to_cents(pick(row, KEG_FIELDS["price_pot_team"]), money_unit,
                                   context="fût (prix pot équipe)"),
    }


def map_tap_row(row):
    """Convertit une occupation de tireuse (Brest `draft_beer_current`)."""
    return {
        "src_id": pick(row, TAP_FIELDS["src_id"]),
        "draught": pick(row, TAP_FIELDS["draught"]),
        "src_keg_id": pick(row, TAP_FIELDS["keg_id"]),
        "date_start": parse_dt(pick(row, TAP_FIELDS["date_start"])),
        "date_end": parse_dt(pick(row, TAP_FIELDS["date_end"])),
    }
