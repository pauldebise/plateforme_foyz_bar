# Plateforme Foy'z & Bar — ENSTA Bretagne & ENSTA Paris

Plateforme web unifiée de gestion des Foy'z/Bar des deux campus : interface publique, interface
équipe (caisse, comptes étudiants, statistiques, trésorerie) et interface administrateur,
conformément au cahier des charges (`docs/main.tex`).

- Backend : **Python 3 / Flask 3** (architecture modulaire en blueprints)
- Base de données : **SQLAlchemy 2** — SQLite en développement, PostgreSQL en production
- Front-end : **Bootstrap 5 + Chart.js** (CDN), templates Jinja2, JavaScript vanilla
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
│   ├── services/              # Logique métier (indépendante des routes)
│   │   ├── settings.py        #   Paramètres globaux + mot de passe administrateur
│   │   ├── transactions.py    #   Moteur de caisse : achats, consignes, annulations…
│   │   ├── stats.py           #   Statistiques de consommation
│   │   ├── treasury.py        #   Trésorerie mensuelle (12 mois glissants)
│   │   └── catalog.py         #   Gestion fûts/tireuses, génération des articles
│   ├── routes/                # Blueprints
│   │   ├── public.py          #   / (accueil), /catalogue, /reglement, /liens, /trombinoscope
│   │   ├── auth.py            #   /connexion, /deconnexion
│   │   ├── team.py            #   /equipe/* (paiement, opérations, historique, stats…)
│   │   ├── admin.py           #   /admin/* (comptes, équipe, articles, tireuses, événements…)
│   │   ├── gateway.py         #   /passerelle/<token> (encaissement événement)
│   │   └── api.py             #   /api/* (JSON pour l'interface de caisse et les graphiques)
│   ├── templates/             # Jinja2 (public/, team/, admin/, gateway/, errors/)
│   └── static/                # CSS + JS (caisse, recherche d'étudiants, opérations)
├── docs/                      # Cahier des charges
├── uploads/                   # Fichiers téléversés (affiches, logos, PDF, photos)
├── instance/                  # Base SQLite (créée à l'exécution)
├── seed.py                    # Jeu de données de démonstration
├── run.py / wsgi.py           # Lancement dev / point d'entrée Gunicorn
├── requirements.txt
├── Dockerfile
├── docker-compose.yml         # Stack complète : app + PostgreSQL + volumes
├── .env.example
└── COMPTE_RENDU.md            # Compte rendu de réalisation détaillé
```

## 2. Démarrage rapide (développement)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> `psycopg2-binary` (driver PostgreSQL) nécessite Python ≤ 3.13 faute de wheel plus récent.
> En local avec SQLite et Python 3.14, installez les paquets un à un :
> `pip install Flask Flask-SQLAlchemy SQLAlchemy python-dotenv gunicorn`

```bash
flask --app wsgi.py init-db     # crée les tables + le mot de passe administrateur
python seed.py                  # (optionnel) jeu de données de démonstration
python run.py                   # http://127.0.0.1:5000
```

### Comptes de démonstration (après `seed.py`)

| Identifiant        | Mot de passe | Rôle                                |
|--------------------|--------------|-------------------------------------|
| `leo.martin`       | `foyz2026`   | Membre de mandat Brest (accès admin)|
| `camille.rousseau` | `foyz2026`   | Mandat Brest                        |
| `sarah.bernard`    | `foyz2026`   | Mandat Paris                        |
| `antoine.moreau`   | `foyz2026`   | Ancien membre (équipe sans admin)   |
| —                  | `admin`      | Mot de passe **administrateur** (confirmations découvert, annulations…) |

## 3. Déploiement en production

### Option A — Docker Compose (recommandée)

Démarre l'application (Gunicorn, 4 workers) et une base **PostgreSQL 16** persistante.
Les fichiers téléversés (affiches, logos, PDF) sont conservés dans le volume `uploads`.

```bash
cp .env.example .env
# Éditez .env : SECRET_KEY (openssl rand -hex 32) et ADMIN_PASSWORD
docker compose up -d --build
```

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
ExecStart=/opt/foyz/.venv/bin/gunicorn --workers 4 --bind 127.0.0.1:8000 --access-logfile - wsgi:app
Restart=always

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl enable --now foyz

# 5. Nginx (reverse proxy + fichiers statiques)
sudo tee /etc/nginx/sites-available/foyz > /dev/null <<'NGINX'
server {
    listen 80;
    server_name foyz.exemple.fr;

    location /static/ {
        alias /opt/foyz/app/static/;
        expires 7d;
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
dans `.env` (cookies `Secure`).

### Variables d'environnement

| Variable        | Rôle                                                        | Défaut            |
|-----------------|-------------------------------------------------------------|-------------------|
| `FLASK_ENV`     | `production` ou `development`                               | `production`      |
| `SECRET_KEY`    | Signature des sessions — **à changer impérativement**       | valeur de dev     |
| `DATABASE_URL`  | URI SQLAlchemy (SQLite ou PostgreSQL)                       | SQLite locale     |
| `ADMIN_PASSWORD`| Mot de passe administrateur initial (créé par `init-db`)    | `admin`           |
| `HTTPS_ONLY`    | `1` = cookies `Secure` (derrière HTTPS)                     | `0`               |
| `UPLOAD_DIR`    | Dossier des fichiers téléversés                             | `instance/uploads`|

## 4. Sécurité

- Mots de passe hachés (`werkzeug`, scrypt par défaut) ; identifiants jamais en clair.
- Protection **CSRF** sur tous les formulaires et requêtes API (jeton de session).
- Anti-bruteforce sur la connexion (8 tentatives / 5 min par IP).
- Sessions signées, `HttpOnly`, expiration automatique configurable (module développement).
- Mot de passe **administrateur** séparé, requis pour : commandes en découvert,
  annulations de transactions, retrait du statut « blacklist alcool ».
- Registre des connexions (compte, campus, IP, succès/échec) avec purge automatique.
- En-têtes `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`.
- Téléversements restreints (type de fichier, nom sécurisé, dossier dédié).

## 5. Maintenance

- **Sauvegardes** : sauvegardez la base et le dossier des fichiers téléversés :
  - Docker : `docker compose exec db pg_dump -U foyz foyz > backup.sql` + volume `uploads`
  - SQLite : copie de `instance/foyz.db` + `uploads/`
- **Charger la démo sur un déploiement Docker** (optionnel) :
  `docker compose exec web python seed.py`
- **Changer le mot de passe administrateur** : Module développement → Mot de passe administrateur.
- Les paramètres (découvert, consigne, thèmes, durées de conservation…) se règlent dans
  **Administrateur → Module développement**, sans redéploiement.
