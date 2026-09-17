"""Contrat de la base Brest : dump MySQL classique (mysqldump).

Liste blanche des tables métier migrées (les tables de logs, volumineuses,
sont exclues à la volée pendant le streaming — cf. logfilter.py) :

    users              -> membres / étudiants (solde en euros décimaux,
                          + motif de blacklist `blacklist_reason`)
    transactions       -> achats (historique comptable conservé, justifie les
                          soldes) ; la table réelle n'a pas de colonne type :
                          tout est un achat, le détail vient de `baskets`
    baskets            -> lignes de détail des ventes
    payments           -> rechargements (moyen de paiement dans l'enum
                          `type` : CreditCard/Cash/Check/Lydia)
    withdrawals        -> retraits
    transfert          -> transferts, une ligne signée par côté (-X donneur,
                          +X bénéficiaire) : appariées en une transaction
                          cible par (date, opérateur, montant)
    article_types      -> référence des types d'articles (id -> nom) :
                          préchargée pour résoudre le type des articles et
                          des lignes
    articles           -> catalogue (codes-barres, prix public/équipe, volume L)
    draft_beers        -> fûts pressions (cible `kegs` + tarifs `keg_prices`)
    draft_beer_current -> état courant des tireuses (cible `taps` + articles
                          de tireuse générés comme app.services.catalog) ;
                          l'historique d'occupation (date_end non NULL) est
                          ignoré

Schéma réel Brest (dump du 31/08) : users(card_id, name, real_name, password,
balance, blacklisted, is_foyz, promo, disabled, registration, ecocups,
blacklist_reason, alcohol_blacklisted bit(1)), transactions(id, date, user_id,
balance=montant, logged_user_id), baskets(id, transaction_id, article_id,
article_name, article_price, article_type=int, article_volume, quantity,
beer_draught, extra_type), articles(id=code-barre, name, price, price_foyz,
type=int, stock, area, volume, returnable, is_supplyable, store,
nominal_quantity), article_types(id, name), draft_beers(id, name,
half_pint_price[_foyz], pint_price[_foyz], pot_price[_foyz], stock, volume,
alcohol_volume), draft_beer_current(id, beer_draught enum, draft_beer_id,
date_start, date_end), payments(id, date, user_id, balance, type enum,
logged_user_id), withdrawals/transfert(id, date, user_id, balance,
logged_user_id). Aucun email : la clé de réconciliation est l'identité
(`real_name`, sinon le pseudo `name`). L'identifiant de connexion cible est
le nom réel (`real_name`, ex « Paul Debise ») — c'est lui qui servait de
login sur l'ancienne plateforme. Les mots de passe (`password`, hash bcrypt
PHP) sont importés bruts dans `users.legacy_password` : vérifiés à la
connexion puis convertis au format werkzeug. Les identifiants utilisateurs
(`user_id`, `logged_user_id`) sont des badges (users.card_id) : le moteur
les résout en comptes cibles et affiche le nom de l'opérateur quand le
badge est connu. Les tables
`article_draft_beers` / `article_extras` (tarifs par format des tireuses,
extras comptoir) n'ont pas d'équivalent cible : les articles de tireuse sont
régénérés depuis les tarifs des fûts.

Consignes : la colonne `users.ecocups` (verres empruntés) est bien migrée vers
`wallets.glasses_outstanding`, mais elle est HORS du périmètre de l'audit
comptable, qui ne porte que sur les soldes monétaires (R20). Comptes
`disabled` : l'état est conservé et la connexion refusée côté cible (R19).

Ajuster TABLE_MAP / USER_FIELDS / TXN_FIELDS (dans sources/__init__.py) si le
schéma réel diffère la veille de la bascule.
"""

from . import (
    ARTICLE_FIELDS,
    KEG_FIELDS,
    LINE_FIELDS,
    PAYMENT_FIELDS,
    TAP_FIELDS,
    TRANSFER_FIELDS,
    TXN_FIELDS,
    WITHDRAWAL_FIELDS,
    USER_FIELDS,
)

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
    "mouvements": "transactions",
    "payments": "rechargements",
    "paiements": "rechargements",
    "rechargements": "rechargements",
    "withdrawals": "retraits",
    "retraits": "retraits",
    "transfert": "transferts",
    "transferts": "transferts",
    "virements": "transferts",
    "transaction_lines": "lines",
    "vente_lignes": "lines",
    "lignes_vente": "lines",
    "detail_ventes": "lines",
    "détail_ventes": "lines",
    "lignes_transaction": "lines",
    "baskets": "lines",
    "articles": "articles",
    "article": "articles",
    "produits": "articles",
    "produit": "articles",
    "carte": "articles",
    "catalogue": "articles",
    "article_types": "article_types",
    "type_articles": "article_types",
    "draft_beers": "kegs",
    "draft_beer": "kegs",
    "futs": "kegs",
    "fûts": "kegs",
    "kegs": "kegs",
    "draft_beer_current": "taps",
    "tireuses": "taps",
    "taps": "taps",
}

ENTITY_FIELDS = {
    "users": USER_FIELDS,
    "transactions": TXN_FIELDS,
    "rechargements": PAYMENT_FIELDS,
    "retraits": WITHDRAWAL_FIELDS,
    "transferts": TRANSFER_FIELDS,
    "lines": LINE_FIELDS,
    "articles": ARTICLE_FIELDS,
    "article_types": None,
    "kegs": KEG_FIELDS,
    "taps": TAP_FIELDS,
}


def entity_for(table_name):
    """Entité logique migrée pour une table source, None si hors liste blanche."""
    if not table_name:
        return None
    return TABLE_MAP.get(table_name.strip().lower())
