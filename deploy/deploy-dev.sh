#!/usr/bin/env bash
#
# Déploiement de l'environnement de développement sur le VPS Hetzner.
#
# Synchronise l'arbre de travail local vers le VPS, reconstruit l'image et
# redémarre la stack, met à jour APP_VERSION depuis le commit courant, puis
# vérifie la santé du service (origine puis URL publique).
#
# Usage : make deploy-dev   (ou ./deploy/deploy-dev.sh)
#
# Prérequis (à faire une seule fois, cf. AGENT.md) : le VPS doit déjà contenir
#   - $REMOTE_DIR/.env
#   - $REMOTE_DIR/caddy/certs/cert.pem et key.pem (certificat Cloudflare Origin CA)
# Ce script ne touche jamais à ces fichiers : ils sont exclus de la synchro.
set -euo pipefail

SSH_HOST="${FOYZ_SSH_HOST:-hetzner-site}"
REMOTE_DIR="${FOYZ_REMOTE_DIR:-/home/deploy/app}"
DOMAIN="${FOYZ_DOMAIN:-dev.foyz.fr}"
APP_PORT="${FOYZ_APP_PORT:-8000}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Version applicative : commit courant, suffixée « -dirty » si l'arbre local
# contient des modifications non commitées (cas fréquent en développement).
APP_VERSION="dev-$(git rev-parse --short HEAD)"
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
  APP_VERSION="${APP_VERSION}-dirty"
fi

echo "==> Cible : $SSH_HOST:$REMOTE_DIR  (version : $APP_VERSION)"

echo "==> Vérification des fichiers de déploiement sur le VPS"
if ! ssh "$SSH_HOST" "test -f '$REMOTE_DIR/.env' && test -f '$REMOTE_DIR/caddy/certs/cert.pem' && test -f '$REMOTE_DIR/caddy/certs/key.pem'"; then
  echo "ERREUR : $REMOTE_DIR doit contenir .env, caddy/certs/cert.pem et caddy/certs/key.pem." >&2
  echo "Effectuez d'abord le déploiement initial (certificat Origin CA + .env)." >&2
  exit 1
fi

echo "==> Synchronisation du code source"
# Exclus : dépôt, environnements virtuels, caches, données locales, secrets.
# caddy/ et docker-compose.override.yml sont gérés séparément (server-only).
rsync -az --delete \
  --exclude='.git/' --exclude='.venv/' --exclude='venv/' \
  --exclude='__pycache__/' --exclude='*.pyc' \
  --exclude='.pytest_cache/' --exclude='.ruff_cache/' \
  --exclude='instance/' --exclude='bdd_a_migrer/' --exclude='archives/' \
  --exclude='.env' --exclude='caddy/' --exclude='docker-compose.override.yml' \
  --exclude='*.log' --exclude='nohup.out' --exclude='.DS_Store' \
  ./ "$SSH_HOST:$REMOTE_DIR/"

echo "==> Mise à jour de la configuration de reverse-proxy"
ssh "$SSH_HOST" "mkdir -p '$REMOTE_DIR/caddy'"
rsync -az deploy/docker-compose.override.yml "$SSH_HOST:$REMOTE_DIR/docker-compose.override.yml"
rsync -az deploy/Caddyfile "$SSH_HOST:$REMOTE_DIR/caddy/Caddyfile"

echo "==> Mise à jour de APP_VERSION dans .env"
ssh "$SSH_HOST" "cd '$REMOTE_DIR' && if grep -q '^APP_VERSION=' .env; then sed -i 's|^APP_VERSION=.*|APP_VERSION=$APP_VERSION|' .env; else printf 'APP_VERSION=%s\n' '$APP_VERSION' >> .env; fi"

echo "==> Reconstruction et redémarrage des conteneurs"
ssh "$SSH_HOST" "cd '$REMOTE_DIR' && docker compose up -d --build"

echo "==> Contrôle de santé (origine sur le VPS)"
ok=0
for _ in $(seq 1 30); do
  if ssh "$SSH_HOST" "curl -fsS 'http://127.0.0.1:$APP_PORT/health' >/dev/null 2>&1"; then
    ok=1
    break
  fi
  sleep 2
done
if [ "$ok" != "1" ]; then
  echo "ERREUR : l'origine ne répond pas sur 127.0.0.1:$APP_PORT/health." >&2
  ssh "$SSH_HOST" "cd '$REMOTE_DIR' && docker compose ps" >&2 || true
  exit 1
fi
echo "  origine OK"

echo "==> Contrôle public https://$DOMAIN/health"
public_code=""
for _ in $(seq 1 10); do
  public_code="$(curl -sS -o /dev/null -w '%{http_code}' "https://$DOMAIN/health" 2>/dev/null || true)"
  [ "$public_code" = "200" ] && break
  sleep 3
done
if [ "$public_code" != "200" ]; then
  echo "ATTENTION : https://$DOMAIN/health a renvoyé « ${public_code:-échec} »." >&2
  echo "Vérifiez le DNS/résolveur local ; l'origine est saine." >&2
  exit 1
fi
echo "  public OK (HTTP 200)"

echo
echo "Déploiement terminé : https://$DOMAIN  (version $APP_VERSION)"
