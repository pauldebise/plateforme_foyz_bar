# Plan d'action — Plateforme Foy'z & Bar

**Source** : `AUDIT_TECHNIQUE.md` (constats S1-S20, R1-R21, P1-P7, U1-U11, D1-D15, §7.1 Cloudflare)
**Date** : 16 septembre 2026
**Statut git du présent fichier** : non suivi (à supprimer après usage).
**Règle** : aucune modification de code n'est effectuée ici ; ce document décrit les chantiers, leur ordre, leurs critères d'acceptation et de vérification.

---

## 0. Mode d'emploi et conventions

- **ID** : `T-<phase>.<n>` (ex. `T-1.3`).
- **Priorité** : `P0` bloquant production, `P1` à traiter sous quelques semaines, `P2` amélioration.
- **Estimation** : ordre de grandeur en **jours-homme** (j/h), à ajuster selon le nombre de personnes. Fourchette basse/haut.
- **Critère d'acceptation (DoD)** : ce qui doit être vrai pour clore la tâche.
- **Vérif** : comment on prouve que c'est fait (test, commande, manipulation manuelle).
- **Dépendances** : tâches à faire avant.
- **Références** : constats de l'audit couverts.

### Jalons

| Jalon | Contenu | Critère de sortie |
|---|---|---|
| **J0 — Bloquants production** | Secrétisme, passerelle, intégrité soldes, fuite Docker, FK SQLite | Toutes les tâches P0 « critiques » closes + checklist Go/No-Go (§12) au vert |
| **J1 — Durcissement** | XSS, limiteur/IP, uploads, TLS/Cloudflare, données personnelles | P1 sécurité clos |
| **J2 — Exploitabilité** | Sauvegardes, supervision, schéma, CI | Procédure de restauration testée, CI verte |
| **J3 — Confort** | Performance, UX, conformité cahier des charges | P2 priorisés clos |

### Vue d'ensemble des lots

| Lot | Phase | Priorité | Estimation | Constats couverts |
|---|---|---|---|---|
| A | T-1 — Mise en sécurité immédiate | P0 | 4-6 j/h | S1, S2, S3, S7, S8, R2, D1-D4, §7.1 |
| B | T-2 — Remise en état de la passerelle | P0 | 2-3 j/h | R3, U1, U7 |
| C | T-3 — Intégrité comptable et concurrence | P0 | 4-6 j/h | R1, R9, R10, S12 |
| D | T-4 — Sécurité applicative | P1 | 6-9 j/h | S4-S6, S9-S20 |
| E | T-5 — Migration (correctifs critiques) | P1 | 5-8 j/h | R4-R8, R18-R21 |
| F | T-6 — Exploitation et continuité | P1 | 4-6 j/h | D5-D15, R11, R13 |
| G | T-7 — Performance | P1 | 3-5 j/h | P1-P7 |
| H | T-8 — UX, accessibilité, conformité | P2 | 4-6 j/h | U2-U11, R14, R16, §8 |
| I | T-9 — Qualité et tests | P1/P2 | 5-8 j/h | §9 |

---

## 1. Phase 1 — Mise en sécurité immédiate (P0)

### T-1.1 — Interdire les secrets par défaut en production
- **Couvre** : S1, S2, D4.
- **Description** : refuser le démarrage si `FLASK_ENV=production` et que `SECRET_KEY` est absent, court ou égal à une valeur de dev ; refuser aussi `ADMIN_PASSWORD` absent ou égal à `admin`. Journaliser un avertissement explicite en développement, une erreur fatale en production.
- **Étapes** :
  1. Ajouter une validation dans `create_app()` après `from_object(get_config())` (`app/__init__.py:83-97`).
  2. Définir une liste noire de valeurs (`dev-secret-key-change-me`, `change-me-with-a-long-random-string`, `admin`).
  3. Exiger une longueur minimale (ex. 32 caractères) pour `SECRET_KEY` en production.
  4. Mettre à jour `docker-compose.yml` et `.env.example` pour supprimer les valeurs par défaut dangereuses (forcer la fourniture).
- **Fichiers** : `app/__init__.py`, `app/config.py`, `docker-compose.yml`, `.env.example`, `README.md`.
- **DoD** : un `docker compose up` sans `.env` échoue avec un message clair ; un déploiement avec `.env` renseigné démarre.
- **Vérif** : test automatisé `create_app()` avec `FLASK_ENV=production` et `SECRET_KEY` absent → exception ; avec valeur forte → OK.
- **Estimation** : 0,5-1 j/h.

### T-1.2 — Réglages TLS / Cloudflare
- **Couvre** : S7, S11, D4, §7.1 a-b.
- **Description** : garantir que le trafic navigateur→origine est chiffré de bout en bout et que Cloudflare ne peut pas être contourné.
- **Étapes** :
  1. Cloudflare : SSL/TLS = **Full (strict)**, Always Use HTTPS, HSTS.
  2. Vérifier `HTTPS_ONLY=1` dans l'environnement de production.
  3. Restreindre le pare-feu de l'origine aux plages IP Cloudflare (`cloudflare.com/ips`) ou déployer un **Tunnel** et fermer le port 8000.
  4. Option : Authenticated Origin Pulls (mTLS).
- **Fichiers** : documentation de déploiement (`README.md`), configuration serveur (hors dépôt).
- **DoD** : `curl` direct sur l'IP d'origine échoue ; URL HTTPS via Cloudflare fonctionne ; cookie de session marqué `Secure` (visible dans l'inspecteur).
- **Vérif** : test depuis une machine externe + inspection du `Set-Cookie`.
- **Estimation** : 0,5-1 j/h (configuration).

### T-1.3 — Clé d'IP fiable pour le limiteur et les journaux
- **Couvre** : S3, §7.1 c.
- **Description** : ne plus faire confiance au `X-Forwarded-For` brut. Lire `CF-Connecting-IP` uniquement si la requête provient d'une IP Cloudflare (ou via Tunnel) ; sinon `remote_addr`. Purger les compteurs inactifs.
- **Étapes** :
  1. Introduire un helper `client_ip()` dans `app/utils.py`.
  2. Utiliser ce helper dans `auth.login()` (`app/routes/auth.py:62`) et pour `LoginLog.ip`.
  3. Borner/purgeon `_attempts` (TTL + taille max).
  4. Optionnel : mettre en place `ProxyFix` avec `x_for` explicite.
  5. Activer en parallèle la Rate Limiting Cloudflare sur `/connexion` et `/api/*`.
- **Fichiers** : `app/utils.py`, `app/routes/auth.py`.
- **DoD** : envoyer un XFF forgé ne change pas la clé du limiteur ; après 8 échecs réels, la 9e tentive est bloquée quel que soit l'en-tête ; `_attempts` ne croît pas indéfiniment.
- **Vérif** : test unitaire du helper + test d'intégration du limiteur ; test manuel avec en-tête forgé.
- **Estimation** : 1-1,5 j/h.

### T-1.4 — Sécuriser la suppression de compte et les clés étrangères SQLite
- **Couvre** : R2, D7.
- **Description** : corriger le crash de l'historique après suppression d'un compte et aligner dev/prod sur les `ON DELETE`.
- **Étapes** :
  1. Activer `PRAGMA foreign_keys=ON` à chaque connexion SQLite (écouteur `connect`).
  2. Rendre `describe_transaction` tolérant aux contributeurs manquants (`app/services/transactions.py:492-509`).
  3. Vérifier la cohérence des suppressions (`Wallet`, `Contribution`, `Transaction.deposit_user_id`/`from`/`to`).
  4. Revoir le libellé « anonymisé » de `admin.compte_supprimer` (`app/routes/admin.py:192-195`) ou anonymiser réellement (`operator_label`, `LoginLog.name`, notes).
- **Fichiers** : `app/__init__.py`, `app/services/transactions.py`, `app/routes/admin.py`.
- **DoD** : après suppression d'un compte avec historique, la page Historique s'affiche sans erreur en SQLite ; aucune contribution orpheline ; message conforme à la réalité.
- **Vérif** : reproduire le scénario de l'audit (suppression puis `describe_transaction` + chargement `/equipe/historique`).
- **Estimation** : 1-1,5 j/h.

### T-1.5 — Nettoyer le contexte Docker
- **Couvre** : S8, D1.
- **Description** : empêcher toute donnée personnelle et tout volume inutile d'entrer dans l'image.
- **Étapes** :
  1. Compléter `.dockerignore` : `bdd_a_migrer/`, `uploads/`, `tests/`, `.pytest_cache/`, `*.db`, `*.sql`, `*.json*`, `*.csv`, `archives/`.
  2. Passer le conteneur en utilisateur non root (`USER app`) et créer les dossiers avec les bons droits.
  3. Vérifier la taille de l'image après build.
- **Fichiers** : `.dockerignore`, `Dockerfile`.
- **DoD** : `docker build` produit une image < 300 Mo, sans aucun fichier de `bdd_a_migrer/` ni `instance/`.
- **Vérif** : `docker run --rm <image> find / -name "*.sql"` ne retourne rien ; `docker images` pour la taille.
- **Estimation** : 0,5-1 j/h.

### T-1.6 — Rassurer les secrets d'infrastructure
- **Couvre** : D3, D2.
- **Description** : sortir le mot de passe PostgreSQL du Compose et ajouter un healthcheck applicatif.
- **Étapes** :
  1. Utiliser une variable `POSTGRES_PASSWORD` issue de l'environnement (pas de valeur littérale).
  2. Ajouter un healthcheck au service `web` (endpoint `/health` à créer — voir T-6.3, ou simple TCP).
- **Fichiers** : `docker-compose.yml`.
- **DoD** : aucun secret en clair dans `docker-compose.yml`.
- **Vérif** : `grep` de secrets dans les fichiers versionnés.
- **Estimation** : 0,5 j/h.

---

## 2. Phase 2 — Remise en état de la passerelle (P0)

### T-2.1 — Autoriser la lecture étudiants/portefeuille en session passerelle
- **Couvre** : R3, U1.
- **Description** : la passerelle doit pouvoir rechercher un étudiant et consulter son portefeuille, sans donner accès aux autres fonctions équipe.
- **Étapes** :
  1. Faire évoluer la garde de `app/routes/api.py:13-16` : accepter soit `g.current_user`, soit une session passerelle valide (`g.gateway_event`).
  2. Restreindre strictement les endpoints lisibles en passerelle (`/api/students`, `/api/wallet`) et le campus à celui de l'événement.
  3. S'assurer que les écritures passerelle restent cantonnées à `/passerelle/<token>/encaisser`.
- **Fichiers** : `app/routes/api.py`, `app/routes/gateway.py`.
- **DoD** : depuis une session passerelle, la recherche d'étudiant renvoie des résultats et l'encaissement avec contributeurs fonctionne ; les autres endpoints `/api/*` restent 401/403.
- **Vérif** : test automatique simulant le parcours BDE complet (ouverture du lien, recherche, ajout, encaissement).
- **Estimation** : 1-1,5 j/h.

### T-2.2 — Inclure le catalogue standard dans la passerelle
- **Couvre** : U7, §8.
- **Description** : le cahier des charges exige les articles de l'événement **et** le catalogue standard.
- **Étapes** :
  1. Étendre la requête de `gateway()` (`app/routes/gateway.py:40-44`) aux articles standards actifs du campus.
  2. Vérifier `_resolve_items` (qui impose `a.event_id == event_id`) pour autoriser aussi les articles standards en contexte passerelle.
- **Fichiers** : `app/routes/gateway.py`, `app/services/transactions.py`.
- **DoD** : la page passerelle affiche les deux catalogues ; un encaissement mixte (article événement + article standard) fonctionne.
- **Vérif** : test fonctionnel + manipulation manuelle.
- **Estimation** : 0,5-1 j/h.

### T-2.3 — Parcours de sortie de passerelle
- **Couvre** : U1, spec « nouvelle authentification ».
- **Description** : vérifier que « Quitter la passerelle » vide bien la session et renvoie vers la connexion équipe.
- **Vérif** : test manuel : après sortie, `/equipe/paiement` demande la connexion.
- **Estimation** : 0,25 j/h.

---

## 3. Phase 3 — Intégrité comptable et concurrence (P0)

### T-3.1 — Sérialiser les mouvements de solde
- **Couvre** : R1.
- **Description** : supprimer les « lost updates » sur `Wallet.balance`, `Wallet.glasses_outstanding` et `Keg.remaining_l`.
- **Étapes** :
  1. Choisir la stratégie : `SELECT … FOR UPDATE` (PostgreSQL) ou `UPDATE wallets SET balance = balance − :x` atomique, ou colonne de version optimiste.
  2. Encadrer chaque opération (`create_purchase`, `create_reload`, `create_withdrawal`, `create_transfer`, `return_glasses`, `cancel_transaction`) dans une transaction unique avec verrouillage cohérent (ordonner les verrous par `id` pour éviter les interblocages).
  3. Recalculer `balance_after` de façon fiable.
- **Fichiers** : `app/services/transactions.py`, modèles éventuellement.
- **DoD** : deux encaissements simultanés sur le même compte produisent un solde exact (pas de perte), et le découvert maximum reste respecté.
- **Vérif** : test de concurrence (deux threads/sessions) sur SQLite puis PostgreSQL ; scénario « découvert simultané ».
- **Estimation** : 2-3 j/h.

### T-3.2 — Idempotence des encaissements
- **Couvre** : R10, S12.
- **Description** : éviter un double débit en cas de rejeu réseau ou de double soumission.
- **Étapes** :
  1. Générer un jeton d'idempotence côté client par commande (ex. UUID dans `buildPayload`).
  2. Stocker/contrôler ce jeton côté serveur (table ou colonne unique) avant création.
  3. Renvoyer la transaction existante si le jeton est déjà traité.
- **Fichiers** : `app/static/js/payment.js`, `app/routes/api.py`, `app/routes/gateway.py`, `app/services/transactions.py`.
- **DoD** : double POST avec le même jeton = une seule transaction.
- **Vérif** : test API (deux appels identiques).
- **Estimation** : 1-1,5 j/h.

### T-3.3 — Annulation : restitution du fût et cohérence articles
- **Couvre** : R9.
- **Description** : une annulation d'achat doit restaurer le volume du fût et réactiver les articles si nécessaire.
- **Étapes** :
  1. Mémoriser, au moment de la vente, le volume décrémenté par ligne (ou le recalculer à l'annulation).
  2. À l'annulation, ré-incrémenter `remaining_l` et réactiver les articles de tireuse vidés.
- **Fichiers** : `app/services/transactions.py`.
- **DoD** : après annulation, le volume du fût et le catalogue pression sont revenus à leur état d'avant-vente.
- **Vérif** : test service (vente → vidage → annulation → état restauré).
- **Estimation** : 0,5-1 j/h.

### T-3.4 — Verrouiller les encaissements sur la passerelle
- **Couvre** : S12.
- **Description** : limiter le risque d'abus du lien passerelle (endpoint non authentifié).
- **Étapes** : limitation de débit applicative par token/IP, journalisation des encaissements passerelle, alerte en cas de pic.
- **DoD** : seuil de requêtes dépassé = 429 ; les encaissements passerelle sont traçables.
- **Estimation** : 0,5-1 j/h.

---

## 4. Phase 4 — Sécurité applicative (P1)

### T-4.1 — Éradiquer les XSS stockés du front
- **Couvre** : S4, S5.
- **Description** : ne plus injecter de données dans `innerHTML`.
- **Étapes** :
  1. Remplacer les interpolations `r.name`, `c.name`, `w.name`, `a.name`, `group.label` par `textContent`/`createElement` (`app.js`, `payment.js`, `operation.js`).
  2. Échapper côté serveur n'est pas nécessaire si le front est corrigé, mais conserver l'autoescape Jinja.
  3. Restreindre l'upload SVG (retirer des extensions autorisées) ou servir les fichiers avec `Content-Disposition: attachment`.
- **Fichiers** : `app/static/js/app.js`, `payment.js`, `operation.js`, `app/routes/admin.py`, `app/__init__.py`.
- **DoD** : un nom contenant `<img src=x onerror=alert(1)>` s'affiche littéralement, sans exécution.
- **Vérif** : test manuel avec un nom piégé (créé par admin) sur Paiement, recherche, opérations, notes.
- **Estimation** : 1,5-2 j/h.

### T-4.2 — Fin de vie des mots de passe legacy
- **Couvre** : S6.
- **Description** : ne plus conserver de mots de passe en clair / md5 / sha1 sans information des usagers.
- **Étapes** :
  1. Convertir au premier login (déjà en place) et **journaliser** les comptes restants.
  2. Campagne de réinitialisation pour les comptes jamais reconnectés.
  3. Supprimer `legacy_password` une fois la campagne terminée.
- **Fichiers** : `app/routes/auth.py`, éventuellement migration de nettoyage.
- **DoD** : plus aucune valeur en clair/md5/sha1 en base ; `legacy_password` nul partout.
- **Vérif** : requête SQL de contrôle.
- **Estimation** : 1-2 j/h (dépend de l'organisation).

### T-4.3 — En-têtes de sécurité et cache
- **Couvre** : S9, S10, S11, S20.
- **Description** : ajouter les protections manquantes.
- **Étapes** :
  1. `Cache-Control: no-store, private` sur les réponses authentifiées.
  2. `Content-Security-Policy` (adapter aux inline existants ou les extraire).
  3. `SRI` sur les CDN, ou auto-héberger Bootstrap/Icons/Chart.js (recommandé pour la caisse).
  4. HSTS via Cloudflare.
- **Fichiers** : `app/__init__.py`, `app/templates/base.html`, `statistiques.html`, `tresorerie.html`.
- **DoD** : en-têtes présents et valides ; assets locaux disponibles sans Internet.
- **Vérif** : `curl -I` ; test hors ligne.
- **Estimation** : 1-2 j/h.

### T-4.4 — Hygiène des entrées et validations
- **Couvre** : S13, S14, S16, S18, S19.
- **Étapes** :
  1. Borner les longueurs à l'entrée (formulaires) et tronquer avant insertion.
  2. Interdire `inf`/`nan` et bornes numériques dans `cents()` et les formulaires (`_article_from_form`, `_keg_from_form`).
  3. Contrôler le campus pour la suppression de compte (`admin.compte_supprimer`).
  4. Revoir le repli en clair de `check_admin_password` (supprimer le fallback ou le restreindre au dev).
  5. Ne pas construire d'URL de passerelle à partir de `request.host_url` sans validation.
- **Fichiers** : `app/utils.py`, `app/routes/admin.py`, `app/services/settings.py`, `app/templates/admin/evenement.html`.
- **DoD** : formulaires robustes, pas de 500 sur entrée invalide, pas de suppression inter-campus.
- **Vérif** : tests de validation + tests API.
- **Estimation** : 1-1,5 j/h.

---

## 5. Phase 5 — Migration : correctifs critiques (P1)

> Ces correctifs concernent `migration/` ; à traiter avant toute nouvelle bascule.

### T-5.1 — Audit comptable indépendant
- **Couvre** : R4.
- **Description** : calculer la somme source brute (hors mapping) et refuser la migration si `brut − mappé ≠ 0`.
- **Étapes** :
  1. Somme brute des colonnes de solde des sources (y compris comptes non mappés et collisions).
  2. Comparaison stricte globale et par campus ; rapport des clés en collision avec montants.
- **Fichiers** : `migration/audit.py`, `migration/etl.py`.
- **DoD** : sur le dump Brest réel, l'écart de 11,60 € est détecté et bloque.
- **Vérif** : test avec doublons/homonymes porteurs de solde.
- **Estimation** : 1,5-2 j/h.

### T-5.2 — Idempotence et reprise
- **Couvre** : R5, R7, R21.
- **Description** : rendre la bascule rejouable sans duplication et ne rien supprimer d'inutile.
- **Étapes** :
  1. Marqueur de campagne (table/état) refusant un second import.
  2. Nettoyage idempotent (staging + sources).
  3. Matrice campus×extension stricte (Brest = `.sql` uniquement) et refus de supprimer un fichier non consommé.
- **Fichiers** : `migration/cli.py`, `migration/etl.py`, `migration/detect.py`, `migration/cleaner.py`.
- **DoD** : une reprise après échec ne duplique rien ; un fichier non lu n'est jamais supprimé.
- **Vérif** : tests « reprise » et « fichier non consommé ».
- **Estimation** : 2-3 j/h.

### T-5.3 — Ne pas fuiter les hashs dans les logs
- **Couvre** : R8.
- **Étapes** : `create_engine(url, hide_parameters=True)` ; journaliser le type d'erreur sans les paramètres liés.
- **Fichiers** : `migration/cli.py`.
- **DoD** : provoquer une erreur d'insertion n'expose aucun hash/mot de passe dans stderr.
- **Vérif** : test reproduisant une violation d'unicité.
- **Estimation** : 0,25 j/h.

### T-5.4 — Fusion inter-campus et champs manquants
- **Couvre** : R6, R19, R20, R18.
- **Étapes** : clé de fusion alternative validée (nom normalisé + date de naissance, ou fichier de correspondance) ; prise en charge de `disabled` ; documenter l'absence de migration des consignes ; fiabiliser l'appariement des transferts.
- **DoD** : fusion testée sur un dump Brest **sans email** ; comptes désactivés conservés désactivés.
- **Vérif** : fixtures conformes au schéma réel.
- **Estimation** : 2-3 j/h.

---

## 6. Phase 6 — Exploitation et continuité (P1)

### T-6.1 — Sauvegardes automatiques et test de restauration
- **Couvre** : D5.
- **Étapes** : script `pg_dump` + archive du volume `uploads` (cron/systemd timer), rétention (ex. 7 quotidiennes + 4 hebdomadaires), stockage hors serveur ; **procédure de restauration documentée et testée**.
- **DoD** : restauration complète sur environnement vierge en < 1 h.
- **Vérif** : exercice de restauration réel.
- **Estimation** : 1-2 j/h.

### T-6.2 — Permissions et rétention des données
- **Couvre** : D7, D14, D13.
- **Étapes** : `chmod 600 instance/*.db` ; déplacer/supprimer les archives de `bdd_a_migrer/archives/` après rétention ; politique de purge des transactions/logs ; politique de confidentialité minimale.
- **DoD** : aucune donnée personnelle accessible sans droits ; archives purgées après la durée prévue.
- **Vérif** : audit de fichiers + revue.
- **Estimation** : 1 j/h.

### T-6.3 — Supervision et erreurs
- **Couvre** : D6, D10, R11.
- **Étapes** : endpoint `/health` (DB + files d'attente), remontée d'erreurs (Sentry ou logs structurés), rotation des logs, healthcheck Compose, `--max-requests` Gunicorn.
- **DoD** : la page 500 déclenche une alerte ; `/health` répond correctement.
- **Vérif** : coupure DB simulée.
- **Estimation** : 1-2 j/h.

### T-6.4 — Schéma de base versionné
- **Couvre** : R11, D9, D15.
- **Étapes** : introduire Alembic (ou équivalent) ; remplacer `ensure_schema_upgrades` à terme ; documenter la procédure de mise à jour ; retirer `db.create_all()` du démarrage en production.
- **DoD** : une évolution de modèle s'applique par migration versionnée, pas à l'aveugle au boot.
- **Vérif** : migration sur copie de la base réelle.
- **Estimation** : 2-3 j/h.

---

## 7. Phase 7 — Performance (P1)

### T-7.1 — Agréger les statistiques en SQL
- **Couvre** : P1, P2.
- **Étapes** : remplacer les agrégations Python par des `GROUP BY` SQL (`sales_stats`, `students_stats`, `top_articles_stats`, `treasury`) ; ne rapatrier que les agrégats.
- **DoD** : page Statistiques « 1 an » < 1 s et pic mémoire < 100 Mo/worker.
- **Vérif** : rejouer les mesures de l'audit sur la copie de la base réelle.
- **Estimation** : 2-3 j/h.

### T-7.2 — Index et pagination
- **Couvre** : P3, P4, P5, R14.
- **Étapes** : index sur `transaction_lines.article_id` ; pagination de l'historique (et non plafond silencieux) et de la liste des comptes ; éventuellement index trigram pour les recherches.
- **DoD** : pas de scan complet sur les requêtes de classement ; pas de troncature muette.
- **Vérif** : `EXPLAIN`/mesures avant-après.
- **Estimation** : 1 j/h.

### T-7.3 — Cache et assets
- **Couvre** : P6, P7.
- **Étapes** : auto-héberger les assets (ou cache Cloudflare soigné) ; `Cache-Control` adapté sur statiques/uploads ; compression Brotli.
- **DoD** : caisse utilisable sans accès Internet.
- **Vérif** : test hors ligne.
- **Estimation** : 0,5-1 j/h.

---

## 8. Phase 8 — UX, accessibilité, conformité (P2)

### T-8.1 — Corrections UX caisse
- **Couvre** : U2, U3, U4.
- **Étapes** : recalculer catalogue/panier lors de l'ajout/retrait d'un contributeur ; barre d'action collante mobile (total + Encaisser) ; toasts au lieu d'`alert()`.
- **DoD** : le total affiché correspond toujours au débit réel ; sur mobile, encaisser sans remonter.
- **Vérif** : parcours manuel ordinateur + smartphone.
- **Estimation** : 1-2 j/h.

### T-8.2 — Accessibilité
- **Couvre** : U5.
- **Étapes** : listes de résultats focusables (rôle/clavier/ARIA), focus visible, cibles tactiles ≥ 44 px, vérification contrastes.
- **DoD** : parcours complet au clavier et au lecteur d'écran.
- **Vérif** : navigation clavier seule + audit Lighthouse/axe.
- **Estimation** : 1-2 j/h.

### T-8.3 — Écarts au cahier des charges
- **Couvre** : U6, U7, U8, R16, §8.
- **Étapes** : trombinoscopes (public + édition) ; passerelle catalogue standard (déjà T-2.2) ; annulation multiple éventuelle ; ne plus supprimer définitivement les notes (limiter l'affichage).
- **DoD** : tableau §8 de l'audit intégralement « Conforme ».
- **Vérif** : revue fonctionnelle.
- **Estimation** : 2-3 j/h.

---

## 9. Phase 9 — Qualité et tests (P1/P2)

### T-9.1 — Tests applicatifs manquants
- **Couvre** : §9.
- **Étapes** : tests des services de caisse (achat, contributeurs, découvert, consignes, transferts, annulation, concurrence), auth/CSRF/limiteur, autorisations API, passerelle, mode lecture seule, validation.
- **DoD** : couverture des chemins critiques ; tests verts sur SQLite **et** PostgreSQL.
- **Vérif** : exécution de la suite complète.
- **Estimation** : 3-5 j/h.

### T-9.2 — CI/CD
- **Couvre** : D12.
- **Étapes** : workflow (tests, lint `ruff`, format, `pip-audit`, build Docker), pré-commit.
- **DoD** : chaque PR est validée automatiquement.
- **Vérif** : pipeline vert sur le dépôt.
- **Estimation** : 1-2 j/h.

### T-9.3 — Nettoyage de code
- **Couvre** : R17, §9 (assertion vide, code mort).
- **Étapes** : supprimer `allow_negative`, `close_db` vide, duplications `_trim_notes`, dead code migration, assertion `or True`.
- **Estimation** : 0,5-1 j/h.

---

## 10. Backlog non priorisé (P2+)

- MFA applicatif (en complément de Cloudflare Access) et politique de mot de passe plus robuste.
- Journal d'audit des actions d'administration.
- Mode hors ligne/PWA pour la caisse.
- Internationalisation (aujourd'hui français uniquement).
- Tableau de bord « santé » de la plateforme (dernières sauvegardes, espace disque, version).
- Chiffrement au repos de la base et des backups.
- Tests de charge (simulation d'une soirée).

---

## 11. Matrice dépendances (ordre recommandé)

```
T-1.1 ─┐
T-1.2 ─┤
T-1.3 ─┼─> J0 (bloquants prod) ──> T-6.1/T-6.3 (exploitation)
T-1.4 ─┤                         └─> T-5.1->T-5.4 (nouvelle bascule)
T-1.5 ─┤
T-1.6 ─┘
T-2.1 ──> T-2.2 ──> T-2.3
T-3.1 ──> T-3.2
T-3.3 ──> T-3.1 (même service)
T-4.1 ──> T-4.3 (CSP plus simple après extraction des inline)
T-4.2 ──> T-6.2
T-7.1 ──> T-7.2 ──> T-7.3
T-9.1 ──> T-9.2
```

---

## 12. Checklist Go/No-Go avant mise en production

- [ ] `SECRET_KEY` forte et unique en production ; `ADMIN_PASSWORD` fort et changé (`T-1.1`).
- [ ] Cloudflare SSL/TLS = Full (strict), HSTS, `HTTPS_ONLY=1`, origine injoignable hors Cloudflare (`T-1.2`).
- [ ] IP client fiable, limiteur non contournable, compteurs purgés (`T-1.3`).
- [ ] Suppression de compte sans crash, FK cohérentes (`T-1.4`).
- [ ] Image Docker sans données personnelles, non root (`T-1.5`, `T-1.6`).
- [ ] Passerelle fonctionnelle pour le BDE (recherche + encaissement, catalogue standard) (`T-2.1`, `T-2.2`).
- [ ] Soldes corrects en concurrence, encaissements idempotents (`T-3.1`, `T-3.2`).
- [ ] XSS front corrigés, SVG neutralisé (`T-4.1`).
- [ ] Sauvegardes automatiques **et** restauration testée (`T-6.1`).
- [ ] Supervision et endpoint `/health` opérationnels (`T-6.3`).
- [x] Suites de tests applicatives et migration vertes sur PostgreSQL (`T-9.1`, `T-9.2`) — 9 suites applicatives rejouées sur PostgreSQL 16 (schéma Alembic), suites migration/exploitation/performance spécifiques à SQLite.
- [ ] Décision explicite sur les mots de passe legacy (`T-4.2`).

---

## 13. Suivi de progression

| ID | Tâche | Priorité | Statut | Responsable | Échéance | Preuve (PR / test) |
|---|---|---|---|---|---|---|
| T-1.1 | Secrets par défaut | P0 | Fait | | 2026-09-17 | commit f0445a3 ; tests/test_security.py (refus prod, tolérance dev) |
| T-1.2 | TLS / Cloudflare | P0 | Fait (doc) | | 2026-09-17 | commit 2bf7d76 ; README option C + avertissement HTTPS_ONLY ; réglages Cloudflare/pare-feu à appliquer côté serveur |
| T-1.3 | IP fiable / limiteur | P0 | Fait | | 2026-09-17 | commit 4c0fa83 ; tests/test_security.py (XFF forgé ignoré, purge/borne clés, 429) |
| T-1.4 | Suppression compte / FK | P0 | Fait | | 2026-09-17 | commit 112f878 ; test suppression + orphelin hérité |
| T-1.5 | Contexte Docker | P0 | Fait | | 2026-09-17 | commit 3b59c26 ; image 254 Mo, non root, aucun dump embarqué |
| T-1.6 | Secrets infra | P0 | Fait | | 2026-09-17 | commit 96056cb ; `docker compose config` sans .env → erreur explicite, healthcheck web |
| T-2.1 | Passerelle / recherche | P0 | Fait | | 2026-09-17 | commit 9fc3b58 ; test_gateway : recherche + portefeuille OK, autres endpoints 401/403 |
| T-2.2 | Passerelle / catalogue | P0 | Fait | | 2026-09-17 | commit 4325b67 ; test_gateway : catalogue mixte + encaissement mixte |
| T-2.3 | Sortie passerelle | P0 | Fait | | 2026-09-17 | commit 09ac074 ; test_gateway : session vidée + redirection /connexion |
| T-3.1 | Verrous soldes | P0 | Fait | | 2026-09-17 | commit 61bb388 ; test_concurrency (10 achats concurrents → solde exact ; découvert simultané refusé), vérifié SQLite et PostgreSQL 16 |
| T-3.2 | Idempotence | P0 | Fait | | 2026-09-17 | commits 61bb388 et ed76352 ; test_integrity (rejeu API = 1 transaction, solde débité une fois) ; course PostgreSQL dédupliquée |
| T-3.3 | Annulation fût | P1 | Fait | | 2026-09-17 | commit 61bb388 ; test_integrity : vente → fût vidé → annulation → volume, articles et tireuse restaurés (SQLite et PostgreSQL) |
| T-3.4 | Limiter passerelle | P1 | Fait | | 2026-09-17 | commits ed76352 et b26afe8 ; test_gateway : seuil atteint → 429, alerte journalisée |
| T-4.1 | XSS front | P1 | Fait | | 2026-09-17 | commits eceaeea et 970dd2a ; plus d'interpolation innerHTML (app.js/operation.js/payment.js), SVG retiré des logos, /uploads en `default-src 'none'` ; test_hardening (nom piégé, SVG refusé) |
| T-4.2 | Legacy passwords | P1 | Fait | | 2026-09-17 | commits eceaeea et 970dd2a ; conversion journalisée au login, service legacy_passwords (audit/purge), commande `flask legacy-passwords [--purge]` ; test_hardening (audit + purge + conversion) |
| T-4.3 | En-têtes / cache | P1 | Fait | | 2026-09-17 | commit 970dd2a ; CSP stricte, `Cache-Control: no-store` en session, HSTS si HTTPS_ONLY, assets auto-hébergés (hors ligne) ; test_hardening (en-têtes, assets locaux) |
| T-4.4 | Validation entrées | P1 | Fait | | 2026-09-17 | commit eceaeea ; longueurs bornées, `cents()` refuse inf/nan/hors bornes, suppression inter-campus bloquée, repli admin réservé au dev, lien passerelle sans `request.host_url` ; test_hardening (10 tests) |
| T-5.1 | Audit migration | P1 | Fait | | 2026-09-17 | commit 26bf3d4 ; somme brute vs projetée + collisions, block sur écart ; `--audit-only` sur le dump Brest réel (16/09) → 214 collisions, écart 3 849,08 €, exit 1 ; tests p5_mapping_gap + p5_merge... |
| T-5.2 | Idempotence migration | P1 | Fait | | 2026-09-17 | commit 26bf3d4 ; table `migration_campaign` (marqueur transactionnel), `--force-import`, Brest = `.sql` uniquement, nettoyage refusé si fichier non consommé ; tests p5_replay / p5_brest_rejects_non_sql / p5_dispose |
| T-5.3 | Logs migration | P1 | Fait | | 2026-09-17 | commit 26bf3d4 ; `create_engine(hide_parameters=True)` + `_safe_error` (hashs expurgés) ; tests p5_safe_error / p5_engine_hides |
| T-5.4 | Fusion inter-campus | P1 | Fait | | 2026-09-17 | commits 26bf3d4, 699a930 ; clé email -> nom+naissance -> nom, `disabled` conservé (colonne + login refusé), consignes documentées, transferts ambigus appariés par id ; tests p5_merge_key / p5_disabled / p5_merge_without_email / p5_transfer |
| T-6.1 | Sauvegardes | P1 | Fait | | 2026-09-17 | commit 60b3b54 ; ops/backup+restore (pg_dump/upload, 7+4, rsync, --dry-run) ; exercice réel PostgreSQL 16 : sauvegarde 43 Ko + uploads, destruction (DROP SCHEMA + uploads), restauration, 15 tables/3 comptes/1 transaction/version Alembic retrouvés ; tests test_ops (5) |
| T-6.2 | Permissions / rétention | P1 | Fait | | 2026-09-17 | commits 60b3b54, 95c858c ; base SQLite 600 + instance/ 700, purge login_logs (CLI + R13 sur échec, throttle), purge archives `--purge-archives` (30 j), docs/CONFIDENTIALITE.md ; tests test_monitoring (permissions, purge, R13) |
| T-6.3 | Supervision | P1 | Fait | | 2026-09-17 | commit 95c858c ; `/health` (base/uploads/schéma, 503 dégradé) branché au healthcheck Compose, logs JSON stderr + unhandled_error (trace), rotation Docker 10 Mo x 5, gunicorn timeout/max-requests ; vérifié en stack Compose réelle (`healthy`, /health 200) ; tests test_monitoring (8, dont coupure base simulée) |
| T-6.4 | Schéma versionné | P1 | Fait | | 2026-09-17 | commit 2b21380 ; Alembic + revision 0001_baseline (parité modèles vérifiée SQLite et PostgreSQL 16), plus de DDL au boot PostgreSQL, init-db/upgrade-db, adoption `alembic stamp` testée sur copie de la base réelle (SQLite 182 Mo) et PostgreSQL ; tests test_ops (cycle upgrade/downgrade) |
| T-7.1 | Stats SQL | P1 | Fait | | 2026-09-17 | ventes/étudiants/articles/trésorerie agrégés en SQL (GROUP BY, EXISTS pour les filtres promotion/équipe, CASE pour les mois Paris) ; série journalière en flux de couples (date, montant) ; copie base réelle 465 k lignes / 1 an : sales 4,8 s→0,42 s et 280→0,9 Mo, students 5,8 s→0,39 s / 4,6 Mo, treasury 4,8 s→0,15 s, top 4,5 s→0,08 s (DoD < 1 s / < 100 Mo) ; tests test_performance (valeurs de référence, répartition, TZ) |
| T-7.2 | Index / pagination | P1 | Fait | | 2026-09-17 | index `ix_transaction_lines_article_id` (modèle + révision Alembic 0002, réversible, `alembic check` sans écart sur SQLite et PostgreSQL 16) : EXPLAIN avant SCAN → après SEARCH COVERING INDEX (idem FK SET NULL) ; historique et comptes paginés 50/page avec compteur (plus de plafond 300/200 muet) ; tests test_performance (page 1/2, page 2, comptes) |
| T-7.3 | Cache / assets | P2 | Fait | | 2026-09-17 | assets déjà auto-hébergés (commit 970dd2a) ; `Cache-Control: public, max-age=86400` sur /static et /uploads ; compression Brotli/gzip (Flask-Compress+brotli) vérifiée en-tête `Content-Encoding: br`, `Vary: Accept-Encoding` ; Nginx documenté (gzip/brotli, expires) ; test hors ligne (aucune ressource externe) |
| T-8.1 | UX caisse | P2 | Fait | | 2026-09-17 | U2 : ajout/retrait d'un contributeur recalcule catalogue et panier (plus de décalage total/débit) ; U3 : barre d'action collante mobile (total + Encaisser) caisse et passerelle ; U4 : toasts à la place des alert(), plus de rechargement automatique après vente/consigne ; garde-fous test_ux |
| T-8.2 | Accessibilité | P2 | Fait | | 2026-09-17 | listes de résultats en combobox/listbox ARIA, options focusables et sélectionnables au clavier (flèches/Entrée/Échap, aria-activedescendant, liste vide annoncée) ; anneau de focus visible ; cibles ≥ 44 px sur écran tactile ; étiquettes de connexion (for/id) et favicon local ; audit Lighthouse 100/100 accessibilité et bonnes pratiques sur accueil, catalogue, connexion, trombinoscopes ; test_ux vérifie le markup (parcours lecteur d'écran réel à confirmer sur poste) |
| T-8.3 | Écarts cahier des charges | P2 | Fait | | 2026-09-17 | U6 trombinoscopes (page publique photo/nom/rôle + édition module dev, révision Alembic 0003) ; U7 passerelle avec catalogue standard (déjà T-2.2, vérifié) ; R16 notes limitées à l'affichage mais conservées (« tout afficher ») ; U8 annulation multiple avec un mot de passe ; tableau §8 de l'audit intégralement Conforme |
| T-9.1 | Tests applicatifs | P1 | Fait | | 2026-09-17 | `tests/test_caisse.py` (14 tests : partage/arrondi, prix équipe, découvert + mot de passe admin, blacklists, consignes, rechargements/retraits/transferts, annulations, portée événement, validation, CSRF, autorisations API, lecture seule) ; `tests/run_all.py` : 12 suites / 92 tests vertes en SQLite et 9 suites sur PostgreSQL 16 (schéma recréé puis migré, `make tests-postgres`) ; suites existantes rendues portables (`FOYZ_TEST_DATABASE_URL`, skips SQLite documentés) |
| T-9.2 | CI/CD | P2 | Fait | | 2026-09-17 | `.github/workflows/ci.yml` (qualité ruff check/format + pip-audit ; tests SQLite et PostgreSQL 16 ; `docker build`) ; `.pre-commit-config.yaml` (ruff, ruff-format, compileall) ; `pyproject.toml` (ruff, ligne 100) ; `requirements-dev.txt` ; `make lint/format/audit/tests/tests-postgres` ; vérifié localement : lint+format verts (72 fichiers), `pip-audit` sans vulnérabilité, hooks verts, image Docker construite ; pipeline GitHub à confirmer au premier push |
| T-9.3 | Nettoyage code | P2 | Fait | | 2026-09-17 | suppression de `allow_negative`, `close_db` vide, `_COL_DEF`, `_looks_terminated`, `_FLOAT_RE` ; assertion vide `tests/migration/test_migration.py` remplacée par un contrôle réel ; entrées non numériques (contributeurs, consignes, montants, `user_id`, `link_id`, `position`) refusées proprement (400/404) au lieu d'un 500 — détecté par la suite PostgreSQL |

---

*Plan dérivé de `AUDIT_TECHNIQUE.md`, sans aucune modification du code du projet. Fichier non suivi par git.*
