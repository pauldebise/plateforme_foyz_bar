# Déploiement — Plateforme Foy'z & Bar

Installation et mise en production de la plateforme. Le [README](../README.md)
présente le projet ; l'exploitation courante (sauvegardes, supervision,
rétention) est décrite dans [EXPLOITATION.md](EXPLOITATION.md), la migration des
anciennes bases dans [MIGRATION.md](MIGRATION.md) et l'architecture dans
[ARCHITECTURE.md](ARCHITECTURE.md).

## Option A — Docker Compose (recommandée)

Démarre l'application (Gunicorn, 4 workers) et une base **PostgreSQL 16**
persistante. Les fichiers téléversés (affiches, logos, PDF) sont conservés dans
le volume `uploads`.

```bash
cp .env.example .env
# Éditez .env : SECRET_KEY (openssl rand -hex 32), ADMIN_PASSWORD et
# POSTGRES_PASSWORD (alphanumérique de préférence)
docker compose up -d --build
```

Sans ces variables, `docker compose` refuse de démarrer avec un message
explicite (aucune valeur par défaut faible n'est fournie). Le service `web`
dispose d'un healthcheck HTTP et ne démarre qu'une fois PostgreSQL prêt. La base
et les tables sont créées au premier démarrage (`init-db`), puis Gunicorn
démarre.

L'application est servie sur `http://<serveur>:8000`.

Mise à jour :

```bash
git pull && docker compose up -d --build
```

## Option B — Serveur Linux + Gunicorn + Nginx + PostgreSQL

```bash
# 1. Dépendances
sudo apt install python3-venv python3-pip postgresql nginx
sudo -u postgres psql -c "CREATE USER foyz WITH PASSWORD 'motdepasse-fort';"
sudo -u postgres psql -c "CREATE DATABASE foyz OWNER foyz;"

# 2. Application
sudo mkdir -p /opt/foyz
sudo chown "$USER":"$USER" /opt/foyz
git clone <repo> /opt/foyz
cd /opt/foyz
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

## Option C — Exposition publique via Cloudflare

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

## Variables d'environnement

| Variable        | Rôle                                                        | Défaut            |
|-----------------|-------------------------------------------------------------|-------------------|
| `FLASK_ENV`     | `production` ou `development`                               | `production`      |
| `SECRET_KEY`    | Signature des sessions — **obligatoire en production** (32 car. min.) | valeur de dev (refusée en production) |
| `DATABASE_URL`  | URI SQLAlchemy (SQLite ou PostgreSQL)                       | SQLite locale     |
| `ADMIN_PASSWORD`| Mot de passe administrateur initial des campus actifs (créé par `init-db`) — **obligatoire en production** | `admin` (refusé en production) |
| `FOYZ_ACTIVE_CAMPUSES` | Codes des campus exposés, séparés par des virgules. Production : `brest`. Développement/tests bi-campus : `brest,paris`. | `brest,paris` |
| `HTTPS_ONLY`    | `1` = cookies `Secure` (derrière HTTPS)                     | `0`               |
| `UPLOAD_DIR`    | Dossier des fichiers téléversés                             | `instance/uploads`|
| `TRUSTED_PROXY` | `cloudflare` = IP client lue dans `CF-Connecting-IP`        | vide (aucun)      |
| `PROXY_FIX_X_FOR`| Nombre de proxys de confiance `X-Forwarded-For` (ProxyFix) | `0`               |

> Sur une instance existante, ajouter `FOYZ_ACTIVE_CAMPUSES=brest` au `.env`
> **avant** le redémarrage : sans cette variable, le code retombe sur
> `brest,paris` (mode de compatibilité développement/tests).

## Schéma versionné (Alembic)

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

## Maintenance

- **Sauvegardes automatiques, restauration, supervision, purge** : voir
  [EXPLOITATION.md](EXPLOITATION.md).
- **Changer le mot de passe administrateur** : Module développement → Mot de passe
  administrateur. Ce mot de passe est **propre à chaque campus actif** (il confirme
  les opérations sensibles : découvert, annulation, suppression de compte, retrait
  des statuts blacklist). En production mono-campus, il n'y a qu'un seul secret. À
  la première exécution, `ADMIN_PASSWORD` initialise les campus actifs, et un ancien
  réglage unique (`admin_password_hash`) est repris sur ceux-ci ; un campus
  désactivé conserve son éventuel hash sans être réinitialisé.
- Les paramètres (découvert, consigne, thèmes, durées de conservation…) se règlent
  dans **Administrateur → Module développement**, sans redéploiement.
