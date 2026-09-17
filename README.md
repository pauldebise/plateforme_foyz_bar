# Plateforme Foy'z & Bar — ENSTA Bretagne & ENSTA Paris

Plateforme web unifiée de gestion des Foy'z/Bar des deux campus : interface publique, interface
équipe (caisse, comptes étudiants, statistiques, trésorerie) et interface administrateur,
conformément au cahier des charges (`docs/main.tex`).

- Backend : **Python 3 / Flask 3** (architecture modulaire en blueprints)
- Base de données : **SQLAlchemy 2** — SQLite en développement, PostgreSQL en production
- Front-end : **Bootstrap 5 + Chart.js** auto-hébergés (`app/static/vendor/`, aucune
  dépendance CDN : la caisse fonctionne hors ligne), templates Jinja2, JavaScript vanilla
- Sécurité : sessions signées, CSRF, hachage des mots de passe (scrypt), anti-bruteforce,
  expiration de session par inactivité, mots de passe administrateur pour les opérations sensibles

---

## 1. Arborescence du projet

```
Foyz_plateforme/
├── app/
│   ├── __init__.py            # Factory Flask : sécurité (CSRF, session), filtres, contexte
│   ├── config.py              # Configuration dev / prod via variables d'environnement
│   ├── extensions.py          # Instance SQLAlchemy
│   ├── utils.py               # Constantes métier, helpers temps/monnaie, décorateurs
│   ├── models/                # Modèles SQLAlchemy
│   │   ├── user.py            #   User, Wallet (un portefeuille par campus)
│   │   ├── catalog.py         #   Article, Keg (fût), KegPrice, Tap (tireuse)
│   │   ├── event.py           #   Event (soirée + passerelle)
│   │   ├── transaction.py     #   Transaction, TransactionLine, Contribution
│   │   ├── note.py            #   Note (post-it privé/public)
│   │   └── system.py          #   Setting, LoginLog, UsefulLink
│   ├── services/               # Logique métier (indépendante des routes)
│   │   ├── settings.py        #   Paramètres globaux + mot de passe administrateur
│   │   ├── transactions.py    #   Moteur de caisse : achats, consignes, annulations…
│   │   ├── stats.py           #   Statistiques de consommation
│   │   ├── treasury.py        #   Trésorerie mensuelle (12 mois glissants)
│   │   └── catalog.py         #   Gestion fûts/tireuses, génération des articles
│   ├── routes/                # Blueprints
│   │   ├── public.py          #   / (accueil), /catalogue, /reglement, /liens
│   │   ├── auth.py            #   /connexion, /deconnexion
│   │   ├── team.py            #   /equipe/* (paiement, opérations, historique, stats…)
│   │   ├── admin.py           #   /admin/* (comptes, équipe, articles, tireuses, événements…)
│   │   ├── gateway.py         #   /passerelle/<token> (encaissement événement)
│   │   └── api.py             #   /api/* (JSON pour l'interface de caisse et les graphiques)
│   ├── templates/             # Jinja2 (public/, team/, admin/, gateway/, errors/)
│   └── static/                # CSS + JS (caisse, recherche d'étudiants, opérations)
├── migration/                  # Module ETL de bascule nocturne (voir §6)
├── migrations/                # Révisions de schéma Alembic (voir §6.1)
├── ops/                       # Sauvegarde/restauration (python -m ops.backup, §7)
├── bdd_a_migrer/               # Dumps des anciennes bases (jamais versionnés)
├── tests/                     # Suites exécutables sans pytest (python -m tests.<module>)
├── docs/                      # Cahier des charges + politique de confidentialité
├── uploads/                   # Fichiers téléversés (affiches, logos, PDF)
├── instance/                  # Base SQLite (créée à l'exécution)
├── run.py / wsgi.py           # Lancement dev / point d'entrée Gunicorn
├── alembic.ini                # Configuration Alembic (sans identifiants)
├── requirements.txt
├── Dockerfile
├── docker-compose.yml         # Stack complète : app + PostgreSQL + volumes
└── .env.example
```

## 2. Démarrage rapide (développement)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> `psycopg2-binary` n'est requis que pour la production PostgreSQL
> (inutile en local avec SQLite).

```bash
cp .env.example .env            # puis éditer : FLASK_ENV=development, ADMIN_PASSWORD=<local>
flask --app wsgi.py init-db     # crée les tables + le mot de passe administrateur
python run.py                   # http://127.0.0.1:5000
```

> En production, l'application **refuse de démarrer** si `SECRET_KEY` est absente
> ou trop courte (< 32 caractères), ou si `ADMIN_PASSWORD` est absente ou triviale
> (« admin »). En développement (`FLASK_ENV=development`), ces valeurs par défaut
> sont tolérées mais signalées dans les logs.

### Connexion

Trois champs d'identité distincts :
- **`users.username`** — identifiant de connexion `prenom.nom` (unique, minuscule
  sans accents, séparateurs ramenés à des points), utilisé *exclusivement* pour
  la connexion ; la saisie tolère casse, accents et espaces ;
- **`users.name`** — nom réel complet, base de l'affichage ;
- **`users.nickname`** — surnom d'usage (facultatif, non unique), intégré à
  l'affichage (« Paul Debise (Chips) ») et aux recherches, mais **pas** au login.

Les comptes sont créés par l'administrateur (onglet Comptes) ou importés par le
module de migration (§6).

## 3. Déploiement en production

### Option A — Docker Compose (recommandée)

Démarre l'application (Gunicorn, 4 workers) et une base **PostgreSQL 16** persistante.
Les fichiers téléversés (affiches, logos, PDF) sont conservés dans le volume `uploads`.

```bash
cp .env.example .env
# Éditez .env : SECRET_KEY (openssl rand -hex 32), ADMIN_PASSWORD et
# POSTGRES_PASSWORD (alphanumérique de préférence)
docker compose up -d --build
```

Sans ces variables, `docker compose` refuse de démarrer avec un message
explicite (aucune valeur par défaut faible n'est fournie). Le service `web`
dispose d'un healthcheck HTTP et ne démarre qu'une fois PostgreSQL prêt.

L'application est servie sur `http://<serveur>:8000`.
La base et les tables sont créées automatiquement au premier démarrage ; connectez-vous
ensuite au module développement pour configurer le site (ou chargez la démo, voir §5).

Mise à jour :

```bash
git pull && docker compose up -d --build
```

### Option B — Serveur Linux + Gunicorn + Nginx + PostgreSQL

```bash
# 1. Dépendances
sudo apt install python3-venv python3-pip postgresql nginx
sudo -u postgres psql -c "CREATE USER foyz WITH PASSWORD 'motdepasse-fort';"
sudo -u postgres psql -c "CREATE DATABASE foyz OWNER foyz;"

# 2. Application
cd /opt/foyz && git clone <repo> .
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # renseigner SECRET_KEY, DATABASE_URL, ADMIN_PASSWORD, HTTPS_ONLY=1

# 3. Initialisation
flask --app wsgi.py init-db

# 4. Service systemd
sudo tee /etc/systemd/system/foyz.service > /dev/null <<'UNIT'
[Unit]
Description=Plateforme Foy'z & Bar
After=network.target postgresql.service

[Service]
User=www-data
WorkingDirectory=/opt/foyz
EnvironmentFile=/opt/foyz/.env
ExecStart=/opt/foyz/.venv/bin/gunicorn --workers 4 --bind 127.0.0.1:8000 --access-logfile - --error-logfile - --timeout 60 --graceful-timeout 30 --max-requests 1000 --max-requests-jitter 100 wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl enable --now foyz

# 5. Nginx (reverse proxy + fichiers statiques + compression)
sudo tee /etc/nginx/sites-available/foyz > /dev/null <<'NGINX'
server {
    listen 80;
    server_name foyz.exemple.fr;

    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;
    gzip_min_length 500;
    # brotli on; brotli_types text/css application/javascript application/json;  # module ngx_brotli

    location /static/ {
        alias /opt/foyz/app/static/;
        expires 1d;
    }
    location /uploads/ {
        alias /opt/foyz/uploads/;
        expires 1d;
    }
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
NGINX
sudo ln -s /etc/nginx/sites-available/foyz /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Pour HTTPS, ajoutez un certificat via `certbot --nginx` puis mettez `HTTPS_ONLY=1`
dans `.env` (cookies `Secure`). Derrière un Nginx local, définir aussi
`PROXY_FIX_X_FOR=1` : le limiteur de connexion et le registre des connexions
utilisent ainsi l'IP du visiteur (transmise par Nginx), pas `127.0.0.1`.

### Option C — Exposition publique via Cloudflare

Cloudflare apporte le TLS public, le WAF et la limitation de débit, mais **ne
corrige pas** les défauts applicatifs et introduit ses propres pièges.

1. **SSL/TLS = Full (strict)** obligatoire (jamais « Flexible ») : en Flexible, le
   tronçon Cloudflare → origine circule en clair (identifiants, cookies) tout en
   affichant un cadenas aux visiteurs.
2. Activer **Always Use HTTPS** et **HSTS** (une fois tous les sous-domaines en HTTPS).
3. `HTTPS_ONLY=1` dans `.env` afin que le cookie de session soit marqué `Secure`.
4. **Origine injoignable en direct** : restreindre le pare-feu du serveur aux plages
   d'IP Cloudflare (`https://www.cloudflare.com/ips/`) ou, mieux, déployer un
   **Cloudflare Tunnel** (`cloudflared`) et fermer le port 8000. Sans cela, le WAF
   et la limitation de débit se contournent par l'IP d'origine.
5. Option : **Authenticated Origin Pulls** (mTLS) en complément du pare-feu.
6. **IP réelle du client** : renseigner `TRUSTED_PROXY=cloudflare` (l'application
   lit alors `CF-Connecting-IP` au lieu du `X-Forwarded-For` brut, falsifiable ;
   n'a d'effet sûr qu'avec l'étape 4 appliquée).
7. **Cache** : ne jamais mettre en cache `/equipe/*`, `/admin/*`, `/api/*` ni
   `/passerelle/*` (réponses liées à la session) ; conserver le cache par défaut
   pour `/static/*` et `/uploads/*`.
8. **Cloudflare Access** peut protéger `/admin/*` par SSO/MFA, mais doit exclure
   `/passerelle/*` et les pages publiques sous peine de casser la passerelle.
9. Activer la **Rate Limiting** Cloudflare sur `/connexion` et `/api/*` en défense
   en profondeur (le limiteur applicatif reste nécessaire).

### Variables d'environnement

| Variable        | Rôle                                                        | Défaut            |
|-----------------|-------------------------------------------------------------|-------------------|
| `FLASK_ENV`     | `production` ou `development`                               | `production`      |
| `SECRET_KEY`    | Signature des sessions — **obligatoire en production** (32 car. min.) | valeur de dev (refusée en production) |
| `DATABASE_URL`  | URI SQLAlchemy (SQLite ou PostgreSQL)                       | SQLite locale     |
| `ADMIN_PASSWORD`| Mot de passe administrateur initial (créé par `init-db`) — **obligatoire en production** | `admin` (refusé en production) |
| `HTTPS_ONLY`    | `1` = cookies `Secure` (derrière HTTPS)                     | `0`               |
| `UPLOAD_DIR`    | Dossier des fichiers téléversés                             | `instance/uploads`|
| `TRUSTED_PROXY` | `cloudflare` = IP client lue dans `CF-Connecting-IP`        | vide (aucun)      |
| `PROXY_FIX_X_FOR`| Nombre de proxys de confiance `X-Forwarded-For` (ProxyFix) | `0`               |

## 4. Sécurité

- Mots de passe hachés (`werkzeug`, scrypt par défaut) ; identifiants jamais en clair.
- Protection **CSRF** sur tous les formulaires et requêtes API (jeton de session).
- Anti-bruteforce sur la connexion (8 tentatives / 5 min par IP réelle :
  `CF-Connecting-IP` si `TRUSTED_PROXY=cloudflare`, sinon `remote_addr` —
  `X-Forwarded-For` brut n'est jamais utilisé ; compteurs purgés et bornés).
- Sessions signées, `HttpOnly`, expiration automatique configurable (module développement).
- Mot de passe **administrateur** séparé, requis pour : commandes en découvert,
  annulations de transactions, retrait du statut « blacklist alcool ».
- Registre des connexions (compte, campus, IP, succès/échec) avec purge automatique.
- En-têtes `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`,
  `Content-Security-Policy` stricte (`script-src 'self'`, aucun script inline) ;
  `Cache-Control: no-store, private` sur toutes les pages authentifiées ; HSTS
  quand `HTTPS_ONLY=1`.
- Téléversements restreints (type de fichier, nom sécurisé, dossier dédié) ;
  **SVG refusé** (script exécutable sur l'origine) et fichiers servis avec
  `Content-Security-Policy: default-src 'none'`.
- Entrées bornées (longueurs de colonnes, `cents()` refuse `inf`/`nan` et les
  montants hors limites) ; suppression de compte limitée au campus d'appartenance.
- Mots de passe hérités : conversion automatique au premier login, puis audit et
  purge — `flask --app wsgi.py legacy-passwords [--purge] [--weak-only]`.

## 5. Maintenance

- **Sauvegardes automatiques, restauration, supervision, purge** : voir §7.
- **Changer le mot de passe administrateur** : Module développement → Mot de passe administrateur.
- Les paramètres (découvert, consigne, thèmes, durées de conservation…) se règlent dans
  **Administrateur → Module développement**, sans redéploiement.

## 6. Bascule nocturne : migration des anciennes bases

Le module `migration/` ingère, transforme et injecte les anciennes bases des deux
campus dans la plateforme unifiée, en une transaction PostgreSQL unique avec
audit comptable systématique.

### Déroulé opérationnel

1. Passer les anciens sites en maintenance (gel des écritures).
2. Copier les dumps dans `bdd_a_migrer/` (créé automatiquement) :
   - Brest : dump MySQL `brest_*.sql` **uniquement** (~500 Mo, majoritairement
     des logs — traités en streaming par batchs, sans chargement en RAM) ;
   - Paris : export léger `paris_*.json` / `*.jsonl` / `*.csv` / `*.sql`.
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

### Garanties

- **Montants** : exclusivement en centimes entiers (`INTEGER`), toute source
  flottante ou à fraction de centime est rejetée (`--money-unit euros|cents`).
- **Audit de mapping (indépendant)** : `Σ soldes BRUTS sources = Σ soldes
  projetés` (écart global ET par campus strictement nul). Un compte sans
  identité ou une clé de réconciliation portée par plusieurs lignes (homonymes,
  doublons de carte) **bloque la bascule** avec le détail des clés en collision
  et leurs montants. Sur le dump Brest réel (16/09), cela détecte 214 collisions
  et 3 849,08 € non projetés par l'ancien mapping.
- **Invariable comptable** : `Σ soldes projetés = Σ soldes cibles après − avant`
  (écart global ET par campus strictement nul), vérifié dans la transaction
  avant `COMMIT`.
- **Catalogue** : les articles Brest (codes-barres, prix public/équipe, volumes)
  sont importés avec leur type (table `article_types` ; « Boisson Chaude/Froide »
  rattachées aux consommables `snack`, seul vocabulaire cible non alcoolisé —
  ajustable dans `migration/sources/__init__.py`). Les lignes de vente
  historiques sont rattachées aux articles importés quand c'est possible.
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
- **Comptes désactivés** : `users.disabled` de la source est conservé
  (`disabled` en cible) et la connexion est refusée.
- **Consignes** : `users.ecocups` (verres empruntés) est migré dans
  `wallets.glasses_outstanding` ; hors du périmètre de l'audit **monétaire**.
- **Transferts** : les demi-lignes Brest sont appariées par
  (date, opérateur, montant) — la source n'offre pas de lien explicite ; les
  groupes ambigus sont appariés dans l'ordre des identifiants et comptés au
  rapport.
- **Mots de passe** : les hachages compatibles (werkzeug) sont repris tels
  quels ; les autres (bcrypt de l'ancienne plateforme, md5, texte brut) sont
  importés bruts dans `users.legacy_password` et vérifiés à la connexion
  (puis convertis au format cible au premier login). L'identifiant de connexion
  est le slug `prenom.nom` du nom réel source (`real_name`, ex « Paul Debise »
  -> `paul.debise` ; prénom+nom combinés pour Paris), dédoublonné par suffixe
  numérique en cas d'homonyme. Le pseudo source est conservé en `users.nickname`
  (surnom d'usage, affiché mais inutilisable au login).

### Commandes utiles

```bash
make migrate-audit   # comparaison soldes sources / cibles sans injection (exit 1 si écart)
make migrate-dry     # répétition générale (ROLLBACK + fichiers intacts)
make migrate-run     # bascule réelle
make migrate-keep    # bascule réelle avec archivage des sources
make tests                                 # toutes les suites (SQLite)
make tests-postgres                        # suites applicatives sur PostgreSQL jetable
make lint                                  # ruff check + ruff format --check
python -m tests.migration.test_migration   # suite de tests du module (27 tests)
python -m tests.test_identifiers           # login/identifiants + évolution du schéma
```

Les contrats de mapping (tables/colonnes des anciens schémas) sont centralisés
dans `migration/sources/` — à ajuster si le dump réel diffère, puis relancer un
`--dry-run` pour valider.

### 6.1 Schéma versionné (Alembic)

En production PostgreSQL, le schéma n'est **plus créé au démarrage** de
l'application : il est versionné dans `migrations/versions/` (révision de
référence `0001_baseline`) et appliqué explicitement :

```bash
flask --app wsgi.py upgrade-db    # applique les révisions en attente (idempotent)
flask --app wsgi.py init-db       # upgrade-db + compte administrateur initial
```

- **Installation neuve** : `init-db` crée tout (le `docker compose up` l'appelle
  déjà avant Gunicorn).
- **Base existante** créée par `create_all()` : après sauvegarde, adoptez-la une
  fois — `python -m alembic stamp 0001_baseline` — puis `upgrade-db` pour les
  évolutions suivantes (`0002_article_index`, etc.). `alembic current` doit
  afficher la même révision que `alembic heads` (dernière évolution).
- **Nouvelle évolution** : modifier les modèles puis
  `python -m alembic revision --autogenerate -m "description"`, relire le script
  généré (Alembic ne devine ni les renommages ni les données), le tester sur une
  copie de la base réelle, puis `upgrade-db`.
- SQLite (développement, tests) conserve `create_all()` + évolution automatique
  limitée : Alembic n'est pas requis pour les bases locales jetables.

## 7. Exploitation : sauvegardes, supervision, rétention

### 7.1 Sauvegardes et restauration

`ops/backup.py` réalise un `pg_dump` (format custom) **et** une archive des
téléversements, applique la rétention puis envoie éventuellement hors-site :

```bash
# Cron quotidien (03 h 30) — stack Docker Compose
30 3 * * *  cd /srv/foyz && /srv/foyz/.venv/bin/python -m ops.backup \
  --pg-container foyz-db-1 --pg-user foyz --pg-database foyz \
  --uploads-container foyz-web-1:/data/uploads \
  --backup-dir /srv/backups/foyz --offsite backup@ailleurs:/srv/foyz-backups

# PostgreSQL local (postgresql-client installé)
DATABASE_URL=postgresql://foyz:...@localhost/foyz \
  python -m ops.backup --backup-dir /srv/backups/foyz
```

- Rétention par défaut : **7 copies quotidiennes** + **4 hebdomadaires**
  (`weekly/`). Le script ne supprime que ses propres fichiers `foyz-*.dump[.gpg]`
  / `uploads-*.tgz[.gpg]`.
- `--dry-run` affiche les commandes ; `--prune-only` n'applique que la rétention.

**Chiffrement au repos des sauvegardes** (recommandé) : fournissez un fichier de
phrase de passe (jamais dans le dépôt, permissions `600`, conservé hors du
serveur — gestionnaire de mots de passe ou coffre) :

```bash
openssl rand -base64 32 > /etc/foyz/backup.passphrase
chmod 600 /etc/foyz/backup.passphrase
# Ajoutez --passphrase-file /etc/foyz/backup.passphrase à la commande cron,
# ou BACKUP_PASSPHRASE_FILE=/etc/foyz/backup.passphrase dans l'environnement.
```

Chaque copie est alors chiffrée avec GnuPG (AES-256) et devient
`foyz-*.dump.gpg` / `uploads-*.tgz.gpg` ; l'original en clair est supprimé.
`python -m ops.restore --list` marque les sauvegardes concernées
(`[chiffrée]`) ; la restauration exige la même phrase de passe (sans elle, la
sauvegarde est définitivement illisible : conservez-la en lieu sûr).

La base en elle-même n'est pas chiffrée par l'application : pour le repos,
utilisez le chiffrement du volume (LUKS, disque managé chiffré) — voir §7.3.

Restauration sur un environnement vierge (**procédure testée**, quelques minutes) :

```bash
python -m ops.restore --list --backup-dir /srv/backups/foyz
# Recréer la base cible (rôle et base PostgreSQL), puis :
python -m ops.restore --latest --backup-dir /srv/backups/foyz \
  --pg-container foyz-db-1 --pg-user foyz --pg-database foyz \
  --uploads-container foyz-web-1:/data/uploads --yes
# Sauvegardes chiffrées : ajoutez --passphrase-file /etc/foyz/backup.passphrase
```

La base cible doit exister (par exemple créée par `init-db`) : la restauration
remplace son contenu, elle ne crée pas le rôle. Vérifiez ensuite `/health` et
les totaux (comptes, transactions).

### 7.2 Supervision

- **`GET /health`** : 200 avec `{"status":"ok","database":"ok","uploads":"ok",
  "schema":"ok"}` ; 503 si la base ou les téléversements sont indisponibles.
  Utilisé par le healthcheck Docker Compose.
- **Journaux structurés** : une ligne JSON par événement sur stderr (rotation par
  le pilote Docker `max-size: 10m, max-file: 5`, voir `docker-compose.yml`).
  La page 500 journalise `{"event": "unhandled_error", "path": ..., "exception":
  ...}` : alertez sur `event=unhandled_error` (grep, journald, Sentry côté
  collecteur). `LOG_LEVEL`, `LOG_FORMAT=plain` et `LOG_FILE` (fichier tournant
  10 Mo × 5) sont disponibles.
- **Gunicorn** : `--timeout 60`, `--max-requests 1000` (+ jitter) pour recycler
  les workers, erreurs et accès dans les journaux du conteneur.

### 7.3 Rétention des données personnelles

- **Chiffrement au repos** : les sauvegardes sont chiffrées (GnuPG AES-256) dès
  qu'une phrase de passe est fournie (voir §7.1) ; pour la base et les
  téléversements eux-mêmes, activez le chiffrement du volume (LUKS, disque
  managé chiffré) — l'application ne détient aucune clé de chiffrement.
- Base SQLite : fichier en `600`, dossier `instance/` en `700` (appliqué au
  démarrage).
- Registre des connexions (IP) : purge automatique configurable (90 jours par
  défaut), déclenchée à chaque tentative de connexion et manuellement —
  `flask --app wsgi.py purge-logs [--days N] [--dry-run]`.
- Archives de la migration (`bdd_a_migrer/archives/`, anciennes bases) :
  destruction au-delà de `ARCHIVE_RETENTION_DAYS` (30 jours par défaut) —
  `python -m migration --purge-archives 30`.
- Politique de confidentialité : `docs/CONFIDENTIALITE.md` (à adapter et publier
  sur le site).

### 7.4 Performance (pages statistiques)

- Les statistiques (ventes, étudiants, articles, trésorerie) sont **agrégées en
  SQL** (`GROUP BY`) : la base ne renvoie que les compteurs, jamais les lignes
  brutes. La série journalière reste découpée en Python (heure d'été/hiver) mais
  uniquement sur des couples `(date, montant)` en flux.
- Index `ix_transaction_lines_article_id` (révision Alembic `0002_article_index`)
  pour le classement des articles appelé à chaque encaissement.
- Historique et liste des comptes : **pagination** (50 par page) avec compteur,
  plus de troncature silencieuse.
- Assets servis localement (Bootstrap, icônes, Chart.js) : `Cache-Control` 1 jour
  sur `/static/` et `/uploads/` ; réponses compressées Brotli/gzip par
  l'application (et Nginx le cas échéant). La caisse fonctionne sans Internet.

Mesures sur une copie de la base réelle (465 000 lignes, 1 an) :

| Fonction | Avant | Après |
|---|---|---|
| `sales_stats` | 4,8 s / 280 Mo | 0,42 s / 0,9 Mo |
| `top_articles_stats` | 4,5 s / 279 Mo | 0,08 s / 0,3 Mo |
| `students_stats` | 5,8 s / 270 Mo | 0,39 s / 4,6 Mo |
| `treasury` | 4,8 s / 295 Mo | 0,15 s / 0,3 Mo |

Reproduire : `python -m tests.test_performance` (garde-fous de résultats) et
`EXPLAIN QUERY PLAN` sur la requête de classement.

### 7.5 Tests, lint et intégration continue

- Suites exécutables sans pytest : `make tests` (12 suites, SQLite).
- PostgreSQL 16 jetable : `make tests-postgres` (ou
  `python -m tests.run_all --postgres <url>`). Le schéma public est recréé puis
  migré (`alembic upgrade head`) avant chaque suite — à ne pointer que vers une
  base de test, jamais une base réelle.
- Lint et format : `make lint` (ruff, ligne 100) ; application : `make format`.
- Audit des dépendances : `make audit` (pip-audit sur `requirements.txt`).
- Hooks Git locaux : `pre-commit install` (ruff, ruff-format, compileall).
- CI GitHub Actions (`.github/workflows/ci.yml`) : job qualité (ruff check,
  ruff format --check, pip-audit), job tests SQLite **et** PostgreSQL 16
  (service), job `docker build`. Chaque PR est validée automatiquement.
- Portabilité : les suites migration, exploitation/restauration et performance
  restent spécifiques à SQLite (fichiers sources, copies locales) ; les autres
  tournent sur les deux moteurs.
