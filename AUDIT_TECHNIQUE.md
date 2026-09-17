# Audit technique — Plateforme Foy'z & Bar (ENSTA Bretagne / Paris)

**Date** : 16 septembre 2026
**Périmètre** : intégralité du dépôt (`app/`, `migration/`, `tests/`, `docs/`, Docker, README)
**Contexte de diffusion** : URL publique distribuée via **Cloudflare** (précisé par l'exploitant) — impacts détaillés en §7.1.
**Méthode** : lecture complète du code, exécution des suites de tests, reproductions ciblées sur l'application, mesures de performance sur une copie de la base réelle migrée.
**Statut git du présent fichier** : non suivi (à supprimer après lecture).

---

## 0. Résumé exécutif

L'application est bien structurée, cohérente avec le cahier des charges et couvre l'essentiel du besoin métier. Les fondations de sécurité sont réelles (CSRF global, sessions signées, scrypt, scoping par campus, mot de passe administrateur pour les opérations sensibles). Le module de migration est d'un niveau d'ingénierie inhabituel pour un projet associatif, avec audit comptable et tests.

Cependant, plusieurs points empêchent aujourd'hui une mise en production sereine :

1. **La passerelle événement est fonctionnellement cassée** : la recherche d'étudiants y renvoie systématiquement 401 (reproduit). Seul le paiement direct est utilisable. C'est le cœur du module événement.
2. **Défauts de configuration par défaut dangereux** : `SECRET_KEY` et mot de passe administrateur « admin » si les variables d'environnement ne sont pas définies ; aucun garde-fou au démarrage en production.
3. **Intégrité financière non garantie en concurrence** : les soldes sont modifiés en lecture-écriture sans verrou (lost update) ; deux encaissements simultanés sur le même compte peuvent fausser le solde et le découvert.
4. **Suppression de compte + SQLite = 500 sur l'historique** (reproduit) : les clés étrangères `ON DELETE SET NULL` sont ignorées par SQLite (pragma `foreign_keys=0`), les contributions restent orphelines et `describe_transaction` plante.
5. **Fuite de données dans l'image Docker** : `.dockerignore` n'exclut pas `bdd_a_migrer/` (1,3 Go d'archives de données personnelles actuellement sur le poste) ni `uploads/`, tous deux copiés dans l'image par `COPY . .`.
6. **Audit de migration circulaire** (détaillé en §5) : la somme « source » est calculée à partir du mapping lui-même, donc des soldes peuvent disparaître sans écart détecté (mesuré : 11,60 € sur le dump Brest réel).
7. **XSS stocké possible** dans l'interface équipe via les noms d'étudiants/articles injectés par `innerHTML` (noms issus de l'admin ou de la migration).
8. **Performance de la page Statistiques** : ~5,6 s de CPU et ~350 Mo de pic mémoire par worker sur la base réelle (496 740 transactions) pour un filtre « 1 an », à cause de 3 passes complètes en Python.

> Verdict : **bon socle, pas prêt pour la production en l'état**. Les correctifs P0 (§10) sont peu nombreux et bien identifiés ; une fois traités, l'application peut être déployée sur un serveur maîtrisé (LAN/VPN ou HTTPS).

> **Cloudflare** : il apporte une couche utile (TLS, WAF, anti-DDoS, limitation de débit, éventuellement Access/MFA), mais il **ne corrige pas** les problèmes ci-dessus et introduit ses propres pièges : confiance dans les en-têtes d'IP, mode SSL « Flexible », exposition de l'origine hors Cloudflare, cache de pages authentifiées. À lire impérativement (§7.1).

---

## 1. Contexte et volumétrie observée

| Élément | Valeur constatée |
|---|---|
| Base réelle (`instance/foyz.db`) | 170 Mo, `users` 4 928, `wallets` 4 928, `transactions` 496 740, `transaction_lines` 465 371, `contributions` 498 395 |
| Archives de migration (`bdd_a_migrer/archives/`) | ~1,3 Go (dont un dump Brest de 425 Mo) |
| Portefeuilles en base | tous `brest` (aucun `paris`), cf. §5 |
| Tests | `tests/test_identifiers.py` 3/3 OK ; `tests/migration/test_migration.py` 17/17 OK |
| Runtime local | Python 3.14.4, Flask 3.1.3, SQLAlchemy 2.0.52, SQLite |
| Runtime Docker | `python:3.12-slim`, Gunicorn 4 workers (tzdata présent, testé) |
| Latence mesurée (base réelle, SSD local, SQLite) | Stats 30 j : 0,92 s ; Stats 1 an : 5,6 s ; Trésorerie 12 mois : 1,8 s ; pic RSS ~350 Mo/worker (1 an) |

---

## 2. Points forts

- **Architecture lisible et disciplinée** : factory Flask, blueprints par domaine, services métier indépendants des routes, modèles SQLAlchemy typés.
- **Monnaie en centimes entiers partout** (`INTEGER`), arrondi de part géré (`_split_shares`, `app/services/transactions.py:42`), migration qui refuse les flottants/fractions de centime.
- **Sécurité applicative de base solide** : vérification CSRF centralisée sur toutes les écritures (`app/__init__.py:125-132`), sessions `HttpOnly` + `SameSite=Lax`, expiration par inactivité, `session.clear()` à la connexion (pas de fixation), en-têtes `nosniff`/`X-Frame-Options`/`Referrer-Policy`.
- **Mots de passe** hachés scrypt (Werkzeug) ; reprise des hashs legacy (bcrypt/md5/sha1/brut) avec **conversion automatique au premier login réussi** (`app/routes/auth.py:81-86`).
- **Modèle multi-campus propre** : écritures limitées au campus d'appartenance, l'autre campus consultable en lecture seule avec badge explicite (`app/routes/team.py:38-59`).
- **Mot de passe administrateur séparé** pour les opérations sensibles : découvert, annulation de transaction, retrait du statut blacklist alcool.
- **Registre des connexions** avec purge configurable, anti-bruteforce 8 tentatives/5 min.
- **Uploads** : `secure_filename`, préfixe horodaté aléatoire, liste blanche d'extensions, `MAX_CONTENT_LENGTH` 20 Mo, path traversal bloqué par `send_from_directory`.
- **Module de migration remarquable** : streaming d'un dump de 425 Mo, transaction unique + `ROLLBACK`, audit comptable global ET par campus, refus strict des arrondis, tests fournis.
- **Front-end soigné** : Bootstrap 5 responsive, sidebar repliable, tri du catalogue et des étudiants par popularité récente, recherche instantanée, raccourcis clavier (↑/↓/Entrée/Échap), modales de confirmation, thème par campus.
- **Aucun secret versionné** : `.env` absent du dépôt, `bdd_a_migrer/`, `instance/`, `uploads/*` ignorés par git.
- **Documentation** : README détaillé (dev, Docker, serveur, maintenance, migration).

---

## 3. Sécurité

### 3.1 Critique

| # | Constat | Preuve | Impact |
|---|---|---|---|
| S1 | **`SECRET_KEY` par défaut** `dev-secret-key-change-me` si la variable d'environnement est absente. Docker Compose a un second défaut faible (`change-me-with-a-long-random-string`). Aucun refus de démarrage en production. | `app/config.py:10`, `docker-compose.yml:23` | Forgerie de cookies de session → usurpation de n'importe quel compte, y compris admin. |
| S2 | **Mot de passe administrateur par défaut « admin »** si `ADMIN_PASSWORD` n'est pas défini (config, `.env.example`, Compose). `ensure_dev_admin` le crée aussi à chaque démarrage. | `app/config.py:23`, `app/__init__.py:19-27`, `docker-compose.yml:24`, `.env.example:14` | Prise de contrôle des opérations sensibles (découvert, annulation, déblacklistage). |
| S3 | **Anti-bruteforce contournable** : la clé est `X-Forwarded-For` lu sans proxy de confiance. Derrière Cloudflare, un client peut envoyer son propre `X-Forwarded-For` (Cloudflare l'enrichit mais ne remplace pas la valeur forgée selon la config), donc un attaquant peut changer de clé à chaque requête. `_attempts` n'est jamais purgé pour les IP fictives (croissance mémoire illimitée). Voir §7.1 pour la lecture correcte de `CF-Connecting-IP`. | `app/routes/auth.py:18,47-56,62` | Bourrage de mots de passe illimité ; DoS mémoire trivial ; protection Cloudflare partiellement contournable côté app. |
| S4 | **XSS stocké via `innerHTML`** : `r.name`, `c.name`, `w.name`, `a.name` sont insérés sans échappement dans le DOM. Les noms viennent de l'admin **et de la migration** (dumps legacy). | `app/static/js/app.js:69`, `operation.js:11`, `payment.js:74,249,280` | Un nom piégé (`<img src=x onerror=...>`) s'exécute dans la session d'un membre/admin : vol de session, actions à sa place. |
| S5 | **SVG téléversable comme logo** puis servi sur l'origine : scripts exécutables à l'ouverture directe. | `app/routes/admin.py:40-49,631`, `app/__init__.py:242-244` | XSS stocké (compte mandat requis, mais cible ensuite admin/équipe). |
| S6 | **Mots de passe legacy stockés en clair / md5 / sha1 non salés** dans `users.legacy_password` (comparaison directe en dernier recours). | `app/routes/auth.py:24-44`, `app/models/user.py:24` | Une fuite de base expose des mots de passe réutilisés ailleurs ; les utilisateurs n'en sont pas informés. |
| S7 | **Le TLS est terminé par Cloudflare, mais le lien Cloudflare→origine peut être en clair et l'origine est joignable en direct** : Compose publie 8000 sur toutes les interfaces, `HTTPS_ONLY=0` par défaut (cookie non `Secure`). Si le mode SSL est « Flexible », les identifiants circulent en clair entre Cloudflare et le serveur. | `docker-compose.yml:19-20`, `app/config.py:20`, `README.md:149-175` | Interception sur le tronçon Cloudflare→origine ou en contournant Cloudflare (IP d'origine) ; cookie non `Secure` malgré une URL HTTPS. Voir §7.1. |
| S8 | **Image Docker embarquant les données personnelles** : `.dockerignore` n'exclut ni `bdd_a_migrer/` (1,3 Go) ni `uploads/` ; `COPY . .` les copie dans l'image. | `.dockerignore:1-9`, `Dockerfile:11` | Fuite de données (dumps, hashs) et images énormes si l'image est poussée dans un registre. |

### 3.2 Moyen

| # | Constat | Preuve |
|---|---|---|
| S9 | Pas de `Content-Security-Policy` ; Bootstrap/Icons/Chart.js chargés depuis jsDelivr **sans `integrity` (SRI)**. | `app/templates/base.html:8-9,102`, `statistiques.html:219` |
| S10 | Pas de `Cache-Control: no-store` sur les pages authentifiées (navigation arrière après déconnexion). | `app/__init__.py:185-190` |
| S11 | Pas de HSTS côté application ; à activer côté Cloudflare (une fois tous les sous-domaines en HTTPS). | — |
| S12 | Aucune limitation de débit sur `/api/*` (team) ni sur la passerelle ; aucune protection anti-rejeu/idempotence des encaissements. | `app/routes/api.py`, `app/routes/gateway.py:65-85` |
| S13 | Longueur des entrées non validée (`LoginLog.name` 120 car., `User.name` 255…) : SQLite tronque silencieusement, PostgreSQL lève une erreur 500. | `app/routes/auth.py:93`, `app/models/system.py:19`, `app/models/user.py:15` |
| S14 | **Suppression de compte sans contrôle de campus** : un mandat Brest peut supprimer un compte Paris. | `app/routes/admin.py:180-196` |
| S15 | Politique de mot de passe faible (6 caractères, pas de complexité, pas de verrouillage de compte, pas de MFA). | `app/routes/admin.py:227-234` |
| S16 | Repli de vérification du mot de passe admin en clair si le hash de réglage est absent. | `app/services/settings.py:71-79` |
| S17 | 8 tentatives/5 min **par IP** : derrière un NAT (toute une soirée sur la même box) un changement d'équipe peut se retrouver bloqué ; à l'inverse un attaquant distribué passe. | `app/routes/auth.py:53` |
| S18 | `cents()` accepte `inf`/`nan` (formulaire article non protégé) → 500. | `app/utils.py:61-64`, `app/routes/admin.py:295-307` |
| S19 | Lien de passerelle construit avec `request.host_url` (empoisonnement d'en-tête `Host` possible). | `app/templates/admin/evenement.html:63` |
| S20 | Injection CSS possible via `theme_color_*` (admin uniquement). | `app/templates/base.html:11` |

### 3.3 Faible

- Erreur 400 CSRF non stylée (page Werkzeug brute) : `app/__init__.py:132`.
- Aucun journal d'audit des actions d'administration (changements de prix, réglages, suppressions) : seul le login est journalisé.
- `get_flashed_messages` : pas de `Markup`, donc pas d'injection, RAS.
- `UsefulLink.url` validé par `startswith("http")` : correct, mais mériterait un parseur d'URL strict (http/https uniquement).
- Pas de `robots.txt` / politique de confidentialité / mention RGPD.

---

## 4. Fiabilité & intégrité comptable

### 4.1 Critique

| # | Constat | Preuve | Reproduction |
|---|---|---|---|
| R1 | **Lost update sur les soldes** : lecture puis écriture (`w.balance -= share`) sans verrou ni mise à jour atomique, 4 workers Gunicorn, isolation PostgreSQL `READ COMMITTED` par défaut. Les `balance_after` des contributions peuvent aussi devenir faux. Même problème sur `keg.remaining_l`. | `app/services/transactions.py:221-240,267-273,275-290` | Non reproduit en charge, mais démontrable par analyse (deux transactions lentes concurrentes). |
| R2 | **SQLite n'applique pas les clés étrangères** (`PRAGMA foreign_keys=0` vérifié) : après suppression d'un compte, les contributions restent orphelines et `describe_transaction` plante (`AttributeError: 'NoneType'...`). La page Historique renvoie 500. En PostgreSQL le `ON DELETE SET NULL` fonctionne : divergence dev/prod. | `app/__init__.py` (aucun pragma), `app/models/*`, `app/services/transactions.py:493` | **Reproduit** : suppression d'un compte avec contribution puis `describe_transaction` → crash. |
| R3 | **Passerelle événement inutilisable avec contributeurs** : `/api/students` et `/api/wallet` exigent `g.current_user`, or la route passerelle vide la session et ne pose que `gateway_event_id`. Résultat 401 systématique. | `app/routes/api.py:13-16`, `app/routes/gateway.py:30-38`, `app/static/js/payment.js:416-421` | **Reproduit** : `GET /api/students?q=…` depuis une session passerelle → 401 HTML. |
| R4 | **Audit de migration circulaire** : la somme « source » ne compte que la première occurrence de chaque clé et ignore les comptes non mappés ; côté cible seul le premier compte reçoit un wallet. L'écart est donc nul par construction. Mesuré sur le dump Brest réel : 2 collisions de clés, **11,60 € absents de la somme source sans écart d'audit**. | `migration/etl.py:298-316`, `migration/audit.py:36-49` | Analyse sur `bdd_a_migrer/archives/…/brest_foyz_1609.sql`. |
| R5 | **Migration non idempotente** : aucune détection de « lot déjà migré ». Un échec après `COMMIT` (nettoyage) ou des sources restaurées depuis `archives/` entraînent une duplication comptes/articles/transactions, et l'audit delta la valide. | `migration/cli.py:168-175`, `migration/etl.py:330-337` | Scénarios identifiés par le sous-audit migration. |
| R6 | **Fusion inter-campus inopérante sur le schéma réel** : Brest n'a pas de colonne email, la clé de réconciliation retombe sur le nom ; si Paris a un email, l'intersection est vide. Constat en base : 4 928 wallets, tous `brest`. | `migration/sources/brest.py:40`, `migration/sources/__init__.py:333`, fixture `tests/migration/make_fixtures.py:249-262` (ajoute un email absent du réel) | Requête sur `instance/foyz.db`. |
| R7 | **Fichiers acceptés puis détruits sans être lus** : `brest_*.json/.csv/.jsonl` passent la détection mais seul `sql_dump` est lu pour Brest ; `dispose` supprime/archive ensuite **tous** les fichiers détectés. | `migration/detect.py:74-77`, `migration/etl.py:130-132`, `migration/cleaner.py:30-49` | Analyse. |
| R8 | **Fuites de hashs dans les logs** : `str(exc)` SQLAlchemy imprimé brut contient les paramètres liés (`password_hash`, `legacy_password`). | `migration/cli.py:232,244-247` | Vérifié empiriquement par le sous-audit. |

### 4.2 Moyen

| # | Constat | Preuve |
|---|---|---|
| R9 | **Annulation d'achat sans restitution du fût** : la vente décrémente `remaining_l` et peut vider/désactiver des articles ; l'annulation ne restaure ni le volume ni les articles. | `app/services/transactions.py:267-273` vs `433-478` |
| R10 | Pas d'idempotence des encaissements (double clic géré côté JS uniquement : `paying`) ; un rejeu réseau ou un refresh peut dupliquer un débit. | `app/static/js/payment.js:326-344` |
| R11 | `db.create_all()` + `ensure_schema_upgrades()` + `ensure_dev_admin()` exécutés **à chaque démarrage de chaque worker** (DDL concurrent en PostgreSQL, verrou d'écriture en SQLite). Pas d'Alembic, pas de versionnage de schéma. | `app/__init__.py:94-97`, `docker-compose.yml:31` |
| R12 | `session_timeout_minutes = 0` retombe sur 30 (`or 30`) et une valeur négative expire instantanément. | `app/__init__.py:139` |
| R13 | Purge des `login_logs` déclenchée **uniquement après un login réussi** ; les échecs s'accumulent sans limite tant qu'aucune connexion ne réussit. | `app/routes/auth.py:108,117-126`, `app/routes/admin.py:575-578` |
| R14 | Historique limité à 300 lignes **sans pagination ni indication** de troncature. | `app/routes/team.py:220` |
| R15 | Suppression de compte présentée comme « historique conservé et anonymisé » : en réalité `operator_label`, `LoginLog.name`, notes et archives gardent le nom ; seuls les portefeuilles/contributions sont détachés. | `app/routes/admin.py:192-195` |
| R16 | Les notes publiques au-delà de la limite sont **supprimées définitivement**, pas seulement masquées (le cahier des charges parle de limite d'affichage). | `app/routes/team.py:448-455` |
| R17 | Paramètre `allow_negative` mort ; `close_db` vide ; logique `_trim_notes` dupliquée dans `catalog.py`. | `app/services/transactions.py:150`, `app/__init__.py:181-183`, `app/services/catalog.py:68` |
| R18 | Transferts de migration appariés par `(date, opérateur, montant)` : paires croisées possibles. | `migration/etl.py:616-636` |
| R19 | Colonne `users.disabled` des sources non migrée : des comptes désactivés peuvent se reconnecter. | `migration/sources/brest.py:29`, `migration/sources/__init__.py:124-144` |
| R20 | Consignes/verres historiques (`ecocups`) non migrés ; l'audit ne porte que sur les soldes monétaires. | `migration/sources/brest.py:64-109`, `migration/audit.py:52-69` |
| R21 | `_ensure_schema` committe hors transaction ; le message « base cible inchangée » peut être faux en cas d'échec ultérieur. | `migration/cli.py:116-142,158` |

### 4.3 Faible

- `User.wallet()` crée des portefeuilles sur des requêtes GET (flush non commité) : `app/templates/admin/compte.html:7-9`.
- `keg.remaining_l` en `Float` (comparaisons à 0,01) : `app/models/catalog.py:42`.
- `ensure_dev_admin` réinitialise le mot de passe admin de réglage s'il est absent, sans alerte.
- Textarea des notes toujours éditable : une modification accidentelle est possible (pas de mode lecture).
- README référence `COMPTE_RENDU.md`, supprimé du dépôt : `README.md:58`.

---

## 5. Performance

Mesures sur copie de la base réelle (496 740 transactions, SQLite, SSD local, Python 3.14) :

| Page / requête | Filtre 30 j | Filtre 1 an | Pic RSS |
|---|---|---|---|
| `/equipe/statistiques` (`sales_stats` + `top_articles_stats` + `students_stats`) | 0,92 s | **5,57 s** | ~350 Mo |
| `/equipe/tresorerie` | — | 1,78 s | — |
| `/api/students` (recherche) | < 0,1 s | — | — |

### Constats

| # | Constat | Preuve |
|---|---|---|
| P1 | **Statistiques : 3 passes complètes** sur les mêmes lignes, agrégation en Python, toutes les lignes chargées en ORM. Croissance linéaire du temps et de la mémoire avec l'historique (365 j par défaut). | `app/services/stats.py:55,118,199` |
| P2 | **Trésorerie** : charge tous les rechargements et toutes les lignes de vente des 12 mois en mémoire, puis agrège en Python. | `app/services/treasury.py:36-55` |
| P3 | **Index manquant** sur `transaction_lines.article_id` alors que `top_article_ids` (appelé à chaque page Paiement/passerelle) groupe dessus. | `app/models/transaction.py:40`, `app/services/stats.py:249-269` |
| P4 | Recherches `ILIKE '%q%'` non indexables (étudiants, comptes, journaux). Acceptable à cette volumétrie, à surveiller. | `app/services/transactions.py:78-84`, `app/routes/admin.py:80-85` |
| P5 | Liste des comptes limitée à 200 sans pagination ; recherche recommandée, sinon des comptes sont invisibles. | `app/routes/admin.py:78` |
| P6 | Dépendance totale aux CDN (Bootstrap, icônes, Chart.js) : hors ligne ou CDN lent = interface de caisse dégradée/inutilisable. Pas de SRI. | `app/templates/base.html:8-9,102` |
| P7 | Aucun cache navigateur explicite pour les pages authentifiées ; statiques servis par Flask hors Nginx (Compress/expires absents selon déploiement). | `app/__init__.py:242-244` |

**Bon point** : les relations `lazy="selectin"` évitent le N+1 (mesuré : 6 requêtes pour 50 transactions). Les pages Paiement n'embarquent que le catalogue du campus (volume faible).

---

## 6. Facilité d'utilisation — ordinateur et smartphone

### 6.1 Points forts

- Layout responsive Bootstrap : sidebar repliable avec bouton « Menu » sur mobile, colonnes empilées, `table-responsive` sur tous les tableaux.
- Caisse pensée pour le clavier et le tactile : recherche étudiant instantanée, flèches ↑/↓, Entrée, Échap, tri par popularité, boutons +/- larges, méthodes de paiement direct, modale découvert.
- Parcours clair : filtres par défaut « 30 derniers jours » pour ne pas charger l'historique complet, page de connexion simple, badge « lecture seule » bien visible quand on consulte l'autre campus.
- Confirmation par mot de passe administrateur contextualisée (liste des étudiants qui passeront en négatif).

### 6.2 Problèmes

| # | Constat | Preuve / Impact |
|---|---|---|
| U1 | **Passerelle événement cassée** (R3) : impossible de sélectionner un étudiant ; seul « payer directement » fonctionne. Pour un BDE, c'est bloquant. | Reproduit. |
| U2 | **Prix affichés faux après ajout/retrait d'un contributeur équipe** : le catalogue et le panier ne sont pas recalculés (`renderContributors` seul), le total affiché peut différer du débit réel calculé serveur. | `app/static/js/payment.js:65-87,416-421` |
| U3 | Sur mobile, le bouton **Encaisser** et le total sont au-dessus du catalogue : après avoir ajouté des articles, il faut remonter. Pas de barre d'action collante. | `app/templates/team/payment.html:49-68` |
| U4 | Erreurs affichées en `alert()` navigateur (pas de toasts intégrés) ; rechargement automatique de page 2,6 s après succès (perte de scroll/état). | `payment.js:339,346-359` |
| U5 | Résultats de recherche construits en `<div>` cliquables sans `role`, `tabindex` ni gestion clavier : inaccessible au clavier et aux lecteurs d'écran. | `app/static/js/app.js:62-75` |
| U6 | **Trombinoscopes absents** alors que le cahier des charges les liste (interface publique + paramétrage). | `docs/main.tex:98,145` |
| U7 | La passerelle ne propose pas le **catalogue standard**, uniquement les articles de l'événement, contrairement au cahier des charges. | `docs/main.tex:135` vs `app/routes/gateway.py:40-44` |
| U8 | Historique plafonné à 300 lignes sans pagination ni compteur (R14), annulation une par une (pas de sélection multiple demandée par le cahier des charges). | `team.py:220`, `historique.html` |
| U9 | `autofocus` sur la recherche peut ouvrir le clavier virtuel sur mobile à chaque chargement ; pas d'indicateur de connexion/hors ligne ; pas de PWA. | `payment.html:12` |
| U10 | Pas d'option « afficher le mot de passe » à la connexion, pas de « mot de passe oublié » (reset par un admin uniquement, à documenter). | `auth/login.html` |
| U11 | Comptes : l'année de promotion est un champ nombre libre (pas de validation de plage). | `comptes.html:69` |

**Accessibilité** : contrastes globalement corrects, libellés présents sur la plupart des champs, icônes souvent accompagnées de texte ; à compléter (focus visible, rôles ARIA des listes de résultats, cibles tactiles ≥ 44 px à vérifier).

---

## 7. Déploiement / exploitation

| # | Constat | Preuve |
|---|---|---|
| D1 | **Contexte Docker énorme et fuite de données** : `bdd_a_migrer/` (1,3 Go) et `uploads/` copiés dans l'image ; l'image contiendra les dumps et hashs. | `.dockerignore`, `Dockerfile:11-13` |
| D2 | Conteneur **root**, pas de `USER`, pas de système de fichiers en lecture seule, pas de healthcheck sur le service `web`. | `Dockerfile`, `docker-compose.yml` |
| D3 | Mot de passe PostgreSQL `foyz` en clair dans Compose (réseau interne, mais à externaliser via secrets/env). | `docker-compose.yml:6-7,25` |
| D4 | Pas de reverse proxy TLS dans l'offre Compose ; `HTTPS_ONLY=0` par défaut ; documentation Nginx incomplète sur le durcissement (HSTS, CSP). Cloudflare peut prendre le TLS, mais voir §7.1 (Full strict, firewall origine, `HTTPS_ONLY=1`). | `docker-compose.yml`, `README.md:149-175` |
| D5 | **Sauvegardes manuelles uniquement** (README §5) : pas de cron/pg_dump automatisé, pas de test de restauration, pas de rétention. | `README.md:200-207` |
| D6 | Pas de supervision : aucun endpoint `/health`, pas de Sentry/équivalent ; la page 500 dit « l'équipe technique a été prévenue » alors que rien n'est remonté. | `app/templates/errors/500.html` |
| D7 | **Permissions** : `instance/foyz.db` en 644 (lisible par tout utilisateur local) ; archives de migration 1,3 Go contenant des données personnelles à la racine du projet. | `ls -l instance/` |
| D8 | `Makefile` suppose `.venv/bin/python` à la racine : friction en déploiement système (README utilise `/opt/foyz/.venv`, cohérent, mais le Makefile ne suit pas `PYTHON` par défaut selon l'installation). | `Makefile:3` |
| D9 | Python 3.14 en local vs 3.12 en Docker : OK (tzdata présent, versions épinglées), mais à documenter pour éviter les surprises. | `Dockerfile:1`, `requirements.txt` |
| D10 | Gunicorn : pas de `--timeout`/`--max-requests`, logs d'accès uniquement ; pas de rotation des logs conteneur. | `Dockerfile:19` |
| D11 | `psycopg2-binary` déconseillé en production par les mainteneurs (préférer `psycopg[binary]` ou compilation). | `requirements.txt:11` |
| D12 | **Aucune CI/CD** : pas de workflow, pas de lint/format configuré (ruff/black absents), pas de scan de vulnérabilités (`pip-audit`). Les tests ne tournent que manuellement. | dépôt |
| D13 | **RGPD / données personnelles** : noms, soldes, historique, logs IP conservés sans durée de purge globale ; archives de migration et anciens hashs conservés ; pas de politique de confidentialité ni de registre. À traiter même pour un projet associatif. | transversal |
| D14 | Le module de migration peut supprimer/archiver les sources ; les archives restent indéfiniment dans `bdd_a_migrer/archives/` (données personnelles). Prévoir une destruction sécurisée après la période de rétention. | `migration/cleaner.py:16-49` |
| D15 | README : références obsolètes (`COMPTE_RENDU.md`), pas de procédure de mise à jour de schéma (le schéma est « migré » à l'exécution par `ensure_schema_upgrades`). | `README.md:58,107-111` |

---

## 7.1 Diffusion via Cloudflare — impacts et réglages recommandés

Cloudflare est une bonne décision pour l'exposition publique (TLS automatique, WAF, anti-DDoS, limitation de débit, filtrage géographique, éventuellement Access). Il faut néanmoins savoir que **rien de ce qui précède n'est corrigé par Cloudflare**, et que plusieurs réglages sont nécessaires pour ne pas créer une fausse impression de sécurité.

### a) TLS et cookies

- **Mode SSL/TLS = Full (strict)** obligatoire. Le mode **Flexible** laisserait le tronçon Cloudflare→origine en HTTP clair : identifiants, cookies de session et mot de passe administrateur y circuleraient en clair, tout en affichant un cadenas aux visiteurs. C'est le piège le plus fréquent.
- Activer **Always Use HTTPS** et **HSTS** (dans Cloudflare, une fois certain que tous les sous-domaines sont en HTTPS).
- Définir **`HTTPS_ONLY=1`** dans l'environnement de l'application, sinon le cookie de session n'est pas `Secure` (`app/config.py:20`) malgré l'URL HTTPS.
- Idéalement, supprimer complètement le port public de l'origine : **Cloudflare Tunnel** (`cloudflared`) évite d'exposer 8000 et supprime le besoin d'ouvrir un port entrant.

### b) Origine : ne pas pouvoir contourner Cloudflare

- Le WAF, la limitation de débit et le filtrage Cloudflare ne servent à rien si l'IP d'origine est connue et joignable en direct sur `:8000`. **Restreindre le pare-feu de l'origine aux plages d'IP Cloudflare** (`https://www.cloudflare.com/ips/`) et/ou activer **Authenticated Origin Pulls** (mTLS), ou utiliser un Tunnel.
- Ne pas publier l'IP d'origine (historique DNS, enregistrements MX/SPF, scans) ; changer l'IP si elle a déjà fuité.

### c) IP réelle du client (corrige S3)

- Cloudflare fournit **`CF-Connecting-IP`** (IP réelle du visiteur) et **`X-Forwarded-For`**.
- Le code actuel lit `X-Forwarded-For` brut (`app/routes/auth.py:62`) : un client peut envoyer un XFF forgé, que Cloudflare **conserve en préfixe** puis complète. La clé du limiteur devient donc arbitraire → contournement, et `_attempts` grossit sans limite.
- Correctifs : lire `CF-Connecting-IP` **uniquement si** la connexion provient d'une IP Cloudflare (ou si le Tunnel est utilisé), ou configurer `ProxyFix(x_for=1)` avec `remote_addr` fiable ; normaliser/tronquer l'IP stockée (`LoginLog.ip` 64 car.) ; purger périodiquement `_attempts`.
- En complément, activer la **Rate Limiting Cloudflare** sur `/connexion` et `/api/*` : défense en profondeur, mais ne dispense pas du correctif applicatif.

### d) Cache

- Ne **jamais** appliquer une règle « Cache Everything » sur `/equipe/*`, `/admin/*`, `/api/*` ou `/passerelle/*` : ces réponses dépendent de la session (soldes, données personnelles, CSRF). Un cache mal configuré servirait la page d'un étudiant à un autre.
- Laisser le cache par défaut (fichiers statiques) pour `/static/*` et `/uploads/*` ; purger le cache lors d'un changement de logo/thème.
- Le manque de `Cache-Control: no-store` côté app (S10) reste à corriger pour éviter le cache navigateur/bfcache des pages authentifiées.

### e) Limites et timeouts

- Taille d'upload Cloudflare : 100 Mo (Free/Pro) > `MAX_CONTENT_LENGTH` 20 Mo → pas de conflit.
- Timeout proxifié Cloudflare : 100 s (Free/Pro). La page Statistiques mesurée à ~5,6 s passe, mais elle croît avec l'historique ; au-delà de 100 s Cloudflare renverra **524** en masquant la lenteur réelle de l'origine. Corriger P1 (agrégation SQL) avant que l'historique ne grossisse.
- Activer Brotli/compression et HTTP/2 ou HTTP/3 (impact positif sur une liaison mobile en soirée).

### f) Sécurité applicative renforcée par Cloudflare

- **Cloudflare Access** peut protéger `/admin/*` (et éventuellement `/equipe/*`) par SSO/MFA : palliatif bienvenu au manque de MFA de l'application (S15). Attention à **exclure `/passerelle/*`** et la page publique de l'accès protégé, sous peine de casser la passerelle événement.
- **Bot Fight Mode / Managed Challenge** : utile contre les robots, mais peut gêner l'usage tablette de la caisse ; prévoir des exceptions par chemin si nécessaire.
- Une **CSP** peut être ajoutée via Cloudflare Transform Rules, mais l'application utilise des scripts et styles **inline** (`base.html:11`, `statistiques.html:220`, `tresorerie.html:72`) : la CSP devra soit autoriser `'unsafe-inline'` (protection partielle), soit être précédée d'un nettoyage des inline.

### g) Journalisation

- Les logs applicatifs (`LoginLog`) enregistrent l'IP : s'assurer qu'elle provient bien de `CF-Connecting-IP` (sinon on stocke une chaîne XFF falsifiable et trop longue).
- Exploiter les analytics/Logpush Cloudflare pour détecter les pics 401/403/429 (bourrage, scans) et croiser avec le registre applicatif.

---

## 8. Conformité au cahier des charges (`docs/main.tex`)

| Exigence | État |
|---|---|
| Interface publique : accueil, prix standards, règlement, liens utiles, page d'identification | Conforme |
| **Trombinoscopes (présentation des équipes) + édition** | Conforme (phase 8 : page publique + édition dans le module dev) |
| Interface équipe : paiement, contributeurs multiples, blacklist/alcool, découvert avec mot de passe admin, paiement direct | Conforme |
| Consignes activables, suivi et restitution | Conforme |
| Rechargement (4 moyens), retrait, transfert avec contrôle de solde | Conforme |
| Historique filtres/recherche, annulation avec mot de passe admin | Conforme (pagination phases 7-8, annulation multiple U8) |
| Statistiques filtrables (période, catégorie, promo, campus, équipe) | Conforme (performances à surveiller) |
| Trésorerie 12 mois, rechargements par moyen, ventes par type, événements, graphe | Conforme |
| Post-it privés/publics avec limites d'affichage | Conforme (limite d'affichage seulement, notes conservées — R16 corrigé phase 8) |
| Admin : comptes, équipe, articles, tireuses, événements, journaux, module dev | Conforme |
| Passerelle événement = articles événement **+ catalogue standard**, sans accès admin | Conforme (phases 2 : catalogue standard, recherche étudiants, sortie de passerelle) |
| Anciens membres : accès équipe conservé, admin retiré | Conforme (`admin.py:31-36`) |
| Blacklist mutualisée, accès retirés, alcool conservé sauf admin | Conforme |
| Registre de connexions avec purge configurable | Conforme |
| Config : découvert, consigne, PDF, accueil, thèmes/logos, durée d'accès, timeout, mot de passe admin, post-its, liens, trombinoscopes | Conforme |

*Tableau mis à jour le 2026-09-17 à l'issue de la phase 8 (U6, U7, U8, R16) ;
les constats d'origine restent décrits aux §6.2 et §7.*

---

## 9. Tests et qualité

- **État après la phase 9 (2026-09-17)** :
  - `make tests` : **12 suites / 92 tests verts** en SQLite ; `make tests-postgres` :
    **9 suites vertes sur PostgreSQL 16** (schéma recréé puis migré par Alembic
    avant chaque suite) ;
  - `tests/test_caisse.py` (14 tests) : partage des montants (arrondi exact),
    prix équipe, découvert + mot de passe administrateur, blacklists, consignes,
    rechargements/retraits/transferts, annulations (achat, consigne, direct,
    rechargement, retrait, transfert), portée des articles d'événement,
    validation (aucune entrée ne produit un 500), CSRF, autorisations API,
    lecture seule de l'autre campus ;
  - couverture déjà en place : migration (27), sécurité (9), passerelle (3),
    intégrité (3), concurrence (2), durcissement (11), exploitation (5),
    supervision (8), performance (3), interface (4), identifiants (3) ;
- **Résolus** : assertion vide de `tests/migration/test_migration.py` remplacée
  par un contrôle réel ; code mort supprimé (`_COL_DEF`, `_looks_terminated`,
  `_FLOAT_RE`, `allow_negative`, `close_db` ; `_trim_notes` supprimé en phase 8) ;
  entrées non numériques (`user_id`, `link_id`, `position`, contributeurs,
  consignes, montants) refusées en 400/404 au lieu d'un 500 (détecté par la
  suite PostgreSQL) ; lint/format `ruff`, `pip-audit`, hooks `pre-commit` et CI
  GitHub Actions (qualité, tests SQLite + PostgreSQL, image Docker).
- **Restes assumés** : pas de test de bout en bout navigateur (parcours clavier
  et lecteur d'écran à confirmer sur poste), pas de test de charge « soirée »,
  suites migration/exploitation/performance volontairement SQLite.

---

## 10. Plan d'action priorisé

### P0 — avant toute mise en production

1. **Refuser le démarrage en production** si `SECRET_KEY` est absent/faible ou si `ADMIN_PASSWORD` vaut « admin » (`app/config.py`, `app/__init__.py`), et générer les valeurs par défaut dans Compose.
2. **Corriger la passerelle** : autoriser la lecture étudiants/portefeuille en session passerelle (ex. `g.gateway_event`), inclure le catalogue standard, tester le parcours complet BDE.
3. **Sérialiser les mouvements de solde** : `SELECT … FOR UPDATE` sur `Wallet` (et `Keg`) ou `UPDATE … SET balance = balance ± :x` atomique + verrou optimiste ; écrire des tests de concurrence.
4. **Supprimer la fuite Docker** : compléter `.dockerignore` (`bdd_a_migrer/`, `uploads/`, `tests/`, `.pytest_cache/`), et vérifier la taille de l'image.
5. **FK SQLite** : activer `PRAGMA foreign_keys=ON` à la connexion (ou rendre `describe_transaction` tolérant aux utilisateurs manquants) ; corriger le libellé « anonymisé » de la suppression.
6. **Audit de migration indépendant** : sommer les soldes sources brutes et refuser si `brut − mappé ≠ 0` (collisions + comptes ignorés) ; idempotence (marqueur de campagne) ; `hide_parameters=True` sur le moteur de migration ; ne jamais supprimer un fichier non consommé (matrice campus×extension stricte).
7. **Cloudflare / origine** : SSL/TLS en **Full (strict)** (jamais Flexible), `HTTPS_ONLY=1`, Always Use HTTPS + HSTS ; **restreindre le pare-feu de l'origine aux IP Cloudflare** ou utiliser un Tunnel + Authenticated Origin Pulls ; lire `CF-Connecting-IP` en vérifiant la provenance, ne jamais faire confiance à `X-Forwarded-For` brut. Voir §7.1.

### P1 — sous quelques semaines

8. **XSS** : remplacer les `innerHTML` par `textContent`/`createElement` ou une fonction d'échappement unique côté JS (`app.js`, `payment.js`, `operation.js`).
9. **Limiteur** : clé d'IP fiable (`CF-Connecting-IP` si la requête vient de Cloudflare, ou `ProxyFix` avec un nombre de proxies de confiance défini), purge des comptes/IP inactifs, compteur par identifiant en plus de l'IP. Doubler avec la Rate Limiting Cloudflare.
10. **Mots de passe legacy** : campagne de réinitialisation ou conversion forcée ; ne plus jamais accepter le texte brut après la bascule ; documenter la fin de vie de `legacy_password`.
11. **Performance statistiques/trésorerie** : agréger en SQL (`GROUP BY`), ne charger que les agrégats, ajouter l'index `transaction_lines.article_id`, mettre en cache les réponses du dashboard quelques minutes.
12. **Exploitation** : sauvegardes automatiques (pg_dump + volume uploads, rétention, test de restauration), endpoint `/health`, remontée d'erreurs, rotation des logs, utilisateur non root dans l'image.
13. **Uploads SVG** : servir avec `Content-Disposition: attachment` (ou interdire le SVG), `Cache-Control` adapté.
14. **Purge des archives de migration** et permissions 600 sur `instance/foyz.db`.

### P2 — améliorations

15. UX : barre d'action collante sur mobile, recalcul panier/catalogue quand les contributeurs changent, toasts plutôt qu'`alert()`, pagination de l'historique et des comptes, accessibilité des listes de résultats.
16. Trombinoscopes (public + édition) pour coller au cahier des charges.
17. CI (tests + lint + `pip-audit`), Alembic pour le schéma, tests PostgreSQL et tests de concurrence.
18. Annulation : restitution du fût et réactivation éventuelle des articles ; idempotence des encaissements.
19. Clarté/documentation : README (référence morte, procédure de schéma, politique de mot de passe), HSTS/CSP, mention RGPD et durées de conservation.

---

## 11. Annexe — reproductions et commandes

```bash
# Suites de tests (toutes vertes)
.venv/bin/python -m tests.test_identifiers          # 3/3 OK
.venv/bin/python -m tests.migration.test_migration  # 17/17 OK

# Passerelle : recherche étudiants cassée (401)
# Script de reproduction : GET /passerelle/<token> puis GET /api/students?q=… -> 401
# (création d'un événement en base temporaire, app.test_client)

# Suppression de compte sur SQLite : contributions orphelines puis crash
# PRAGMA foreign_keys -> 0 ; describe_transaction -> AttributeError NoneType

# Mesures performance sur copie de la base réelle (170 Mo, 496 740 transactions)
# Filtre 1 an : sales_stats 1,89 s + top_articles_stats 1,69 s + students_stats 1,99 s
# Pic mémoire ~350 Mo par worker ; trésorerie 1,78 s
```

---

*Rapport généré sans modification du code du projet. Fichier non suivi par git, destiné à être supprimé après prise en compte.*
