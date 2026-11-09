# Migration des anciennes bases — Plateforme Foy'z & Bar

Le module `migration/` ingère, transforme et injecte les anciennes bases dans la
plateforme, en une transaction PostgreSQL unique avec audit comptable
systématique. Cette procédure est ponctuelle ; le schéma courant est versionné
par Alembic (voir [DEPLOIEMENT.md](DEPLOIEMENT.md#schéma-versionné-alembic)).

> **Paris est gelé.** L'import Paris (étape E5 du plan de bascule) n'a jamais été
> exécuté et ne doit plus l'être tant que le campus est inactif. Le code ETL reste
> conservé pour un éventuel retour, mais `FOYZ_ACTIVE_CAMPUSES=brest` rend le
> campus inopérationnel dans l'application.

## Déroulé opérationnel

1. Passer les anciens sites en maintenance (gel des écritures).
2. Copier les dumps dans `bdd_a_migrer/` (créé automatiquement) :
   - Brest : dump MySQL `brest_*.sql` **uniquement** (~500 Mo, majoritairement
     des logs — traités en streaming par batchs, sans chargement en RAM) ;
   - Paris : export léger `paris_*.json` / `*.jsonl` / `*.csv` / `*.sql`. Lot
     réel du 18/09 : deux CSV de caisse, `paris_clients_*.csv` (Nom/Solde
     exploités ; champs postaux ignorés) et `paris_articles_*.csv`
     (Référence/Libellé/Famille/Tarif de base), encodage détecté
     automatiquement (UTF-8 ou CP1252), décimales à la virgule acceptées.
   Toute autre extension (`brest_*.json`, `.zip`, …) est refusée avant
   démarrage : rien n'est lu ni supprimé par surprise.
3. `make migrate-dry` — cycle complet (parsing, conversions, injection, audit) puis
   **ROLLBACK systématique** : la base et les fichiers restent intacts.
4. `make migrate-run` — exécution réelle ; si et seulement si l'audit comptable
   valide un **écart strictement nul (0 centime)**, `COMMIT` puis suppression des
   fichiers sources ; sinon `ROLLBACK` et **aucun fichier n'est touché** (exit 1).
   Option `--keep-archives` (ou `make migrate-keep`) : les sources sont déplacées
   dans `bdd_a_migrer/archives/<horodatage>/` au lieu d'être supprimées.
5. Redémarrer la plateforme.

Le lot migré est marqué dans la table `migration_campaign` (même transaction que
les données) : rejouer exactement le même lot est **refusé** (idempotence) — sauf
`--force-import`, réservé aux experts. Le marqueur est annulé par un `ROLLBACK`,
donc une reprise après échec reste possible.

## Garanties

- **Montants** : exclusivement en centimes entiers (`INTEGER`), toute source
  flottante ou à fraction de centime est rejetée (`--money-unit euros|cents`).
- **Audit de mapping (indépendant)** : `Σ soldes BRUTS sources = Σ soldes
  projetés` (écart global ET par campus strictement nul). Un compte sans
  identité **bloque la bascule**. Une clé de réconciliation portée par
  plusieurs lignes (même nom, plusieurs cartes) est **fusionnée
  automatiquement** dans deux cas, chacun tracé en clair dans le rapport
  (clé, règle, occurrences, montants) :
  - *solde nul* : au plus une occurrence porte un solde non nul (les autres
    cartes sont vides — rien ne peut être attribué à tort, même s'il s'agit
    d'un homonyme) ;
  - *compte ancien* : au moins une occurrence a été créée il y a plus de
    3 ans (compte d'un étudiant parti ; `FUSION_ANCIENNETE_ANNEES` dans
    `migration/etl.py`).
  Le compte fusionné porte la **somme des soldes** et toutes les cartes
  (identifiants source) y sont rattachées, transactions comprises. Dans les
  autres cas (soldes réels des deux côtés sur des comptes récents), la
  décision même personne vs homonymes reste humaine et **bloque la bascule**
  avec le détail des clés en collision. Exception : dans une source sans
  identifiant (CSV clients Paris : Nom/Solde uniquement), les lignes de même
  clé sont le même compte exporté plusieurs fois — leurs soldes sont
  **cumulés** (comptés au rapport) pour préserver l'invariant.
- **Invariable comptable** : `Σ soldes projetés = Σ soldes cibles après − avant`
  (écart global ET par campus strictement nul), vérifié dans la transaction
  avant `COMMIT`.
- **Catalogue** : chaque campus reçoit ses propres articles (`articles.campus`) :
  les catalogues Brest et Paris sont distincts et l'encaissement d'un article
  est limité à son campus. Les articles Brest (codes-barres, prix public/équipe,
  volumes) sont importés avec leur type (table `article_types` ;
  « Boisson Chaude/Froide » rattachées aux consommables `snack`, seul
  vocabulaire cible non alcoolisé — ajustable dans
  `migration/sources/__init__.py`). Les lignes de vente historiques sont
  rattachées aux articles importés quand c'est possible. Les articles de la
  caisse Paris (CSV `Famille`) sont typés (« Bières Bouteilles/Pression » ->
  `biere`, « Vins/alcools » -> `vin`, « Nourriture », « Boissons chaudes/sans
  alcool » -> `snack`, « Z Treso BDE » -> `evenement`) pour que le contrôle
  blacklist alcool reste effectif ; sans tarif membre source, le prix équipe
  vaut le prix public ; les entrées hors vente (« A ne pas ouvrir », « Ne pas
  utiliser », …) sont importées **inactives**.
- **Tireuses** : les fûts (`draft_beers`) deviennent des kegs avec leurs tarifs
  par format (`keg_prices`), et l'état courant des tireuses
  (`draft_beer_current`, lignes non closes, ouverture la plus récente) configure
  les `taps` + régénère les articles de tireuse (demi/pinte/pot) comme le fait
  l'application ; l'historique d'occupation est ignoré.
- **Motifs de blacklist** : `users.blacklist_reason` (Brest) est importé dans la
  colonne `blacklist_reason` de la table `users` (entités HTML décodées) ; la
  colonne est ajoutée automatiquement si la cible préexistante ne l'a pas.
- **Logs exclus** : tables `*_logs`, `log_*`, `connexions`, `sessions`,
  `audit_*`, `debug_*`… purgées au vol ; l'historique **comptable** (transactions,
  ventes) est conservé pour justifier les soldes. Les tables non reconnues ne
  sont **pas** migrées (liste blanche) et figurent au rapport.
- **Réconciliation** : les étudiants présents sur les deux campus (email, sinon
  nom normalisé + date de naissance, sinon nom normalisé) sont fusionnés en un
  compte unique avec deux portefeuilles (un par campus) ; l'historique reste
  rattaché à son `campus` d'origine. La date de naissance (quand elle existe)
  discrimine les homonymes.
- **Comptes désactivés** : un compte désactivé sur toutes ses occurrences
  source n'est **pas migré** (ni compte, ni portefeuille, ni solde à l'audit) ;
  un compte actif sur au moins un campus reste migré normalement.
- **Consignes** : `users.ecocups` (verres empruntés) est migré dans
  `wallets.glasses_outstanding` ; hors du périmètre de l'audit **monétaire**.
- **Statut équipe (mandat / ancien)** : la source Brest ne porte que le drapeau
  `is_foyz` (0/1), vrai pour **tout** membre passé par l'équipe depuis
  l'ouverture — il ne dit pas quel mandat. Les comptes `is_foyz=1` dont
  l'inscription (`registration`) remonte à l'année précédant le mandat en
  cours (ou plus récemment) sont classés `team_status = "mandat"`, les autres
  `"ancien"` ; le campus équipe est déduit (`brest`). L'année du mandat est
  réglée par `MANDATE_YEAR` (`migration/settings.py`, sinon année courante).
- **Transferts** : les demi-lignes Brest sont appariées par
  (date, opérateur, montant) — la source n'offre pas de lien explicite ; les
  groupes ambigus sont appariés dans l'ordre des identifiants et comptés au
  rapport.
- **Mots de passe** : les hachages compatibles (werkzeug) sont repris tels
  quels ; les autres (bcrypt de l'ancienne plateforme, md5, texte brut) sont
  importés bruts dans `users.legacy_password` et vérifiés à la connexion
  (puis convertis au format cible au premier login). L'identifiant de connexion
  est le slug `prenom.nom` du nom réel source (`real_name`, ex « Prénom Nom »
  -> `prenom.nom` ; prénom+nom combinés pour Paris), dédoublonné par suffixe
  numérique en cas d'homonyme. Le pseudo source est conservé en `users.nickname`
  (surnom d'usage, affiché mais inutilisable au login).

## Commandes utiles

```bash
make migrate-audit   # comparaison soldes sources / cibles sans injection (exit 1 si écart)
make migrate-dry     # répétition générale (ROLLBACK + fichiers intacts)
make migrate-run     # bascule réelle
make migrate-keep    # bascule réelle avec archivage des sources
make tests                                 # toutes les suites (SQLite)
make tests-postgres                        # suites applicatives sur PostgreSQL jetable
make lint                                  # ruff check + ruff format --check
python -m tests.migration.test_migration   # suite de tests du module (32 tests)
python -m tests.test_identifiers           # login/identifiants + évolution du schéma
```

Les contrats de mapping (tables/colonnes des anciens schémas) sont centralisés
dans `migration/sources/` — à ajuster si le dump réel diffère, puis relancer un
`--dry-run` pour valider.
