#!/usr/bin/env bash
#
# Envoie la base SQLite locale (instance/foyz.db) et les uploads (instance/uploads)
# vers le VPS, et remplace la base PostgreSQL de dev.foyz.fr.
#
# L'import est atomique côté cible : en cas d'échec, la base dev est restaurée
# par ROLLBACK. Avant toute écriture, la base et les uploads actuels du VPS sont
# sauvegardés dans $REMOTE_BACKUP_DIR.
#
# Usage :
#   make push-data                      (ou ./deploy/push-local-data.sh)
#   ./deploy/push-local-data.sh --yes --clamp-integers
#
# Options :
#   -y, --yes             ne pas demander de confirmation
#       --clamp-integers  borner les entiers hors int32 (au lieu de refuser)
#       --check           vérifie la compatibilité de la source (aucune écriture)
#       --skip-uploads    n'envoyer que la base, laisser les uploads du VPS
#   -h, --help            cette aide
set -euo pipefail

SSH_HOST="${FOYZ_SSH_HOST:-hetzner-site}"
REMOTE_DIR="${FOYZ_REMOTE_DIR:-/home/deploy/app}"
REMOTE_BACKUP_DIR="${FOYZ_BACKUP_DIR:-/home/deploy/backups/foyz}"
REMOTE_TRANSFER_DIR="${FOYZ_TRANSFER_DIR:-/tmp/foyz-transfer}"
LOCAL_DB="${FOYZ_LOCAL_DB:-instance/foyz.db}"
LOCAL_UPLOADS="${FOYZ_LOCAL_UPLOADS:-instance/uploads}"
DOMAIN="${FOYZ_DOMAIN:-dev.foyz.fr}"

ASSUME_YES=0
CLAMP=0
SKIP_UPLOADS=0
CHECK=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) ASSUME_YES=1 ;;
    --clamp-integers) CLAMP=1 ;;
    --check) CHECK=1 ;;
    --skip-uploads) SKIP_UPLOADS=1 ;;
    -h|--help) awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "$0"; exit 0 ;;
    *) echo "Option inconnue : $arg" >&2; exit 2 ;;
  esac
done

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

[ -f "$LOCAL_DB" ] || { echo "Base locale introuvable : $LOCAL_DB" >&2; exit 1; }
if [ "$SKIP_UPLOADS" = "0" ] && [ ! -d "$LOCAL_UPLOADS" ]; then
  echo "Dossier d'uploads local introuvable : $LOCAL_UPLOADS" >&2
  exit 1
fi

echo "Cible    : $SSH_HOST:$REMOTE_DIR"
echo "Base     : $LOCAL_DB"
[ "$SKIP_UPLOADS" = "0" ] && [ "$CHECK" = "0" ] && echo "Uploads  : $LOCAL_UPLOADS"
echo
if [ "$CHECK" = "0" ]; then
  echo "ATTENTION : la base et les uploads actuels de https://$DOMAIN seront REMPLACÉS."
  echo "            Une sauvegarde est créée dans $REMOTE_BACKUP_DIR avant l'import."
  if [ "$ASSUME_YES" != "1" ]; then
    read -r -p "Continuer ? [y/N] " answer
    case "$answer" in
      [yY]|[yY][eE][sS]) ;;
      *) echo "Annulé."; exit 1 ;;
    esac
  fi
fi

PY="$(command -v .venv/bin/python || command -v python3)"

echo "==> Instantané cohérent de la base SQLite (WAL inclus)"
SNAPSHOT="$(mktemp -t foyz-local-XXXXXX.db)"
chmod 644 "$SNAPSHOT"
trap 'rm -f "$SNAPSHOT"' EXIT
"$PY" - "$LOCAL_DB" "$SNAPSHOT" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
PY
echo "    $(du -h "$SNAPSHOT" | cut -f1)"

TS="$(date +%Y%m%d-%H%M%S)"
SNAP_NAME="foyz-local-$TS.db"

echo "==> Préparation du VPS"
ssh "$SSH_HOST" "mkdir -p '$REMOTE_BACKUP_DIR' '$REMOTE_TRANSFER_DIR'"

echo "==> Transfert de l'instantané et du convertisseur"
rsync -az "$SNAPSHOT" "$SSH_HOST:$REMOTE_TRANSFER_DIR/$SNAP_NAME"
rsync -az ops/import_sqlite.py "$SSH_HOST:$REMOTE_DIR/ops/import_sqlite.py"

CLAMP_ARG=""
[ "$CLAMP" = "1" ] && CLAMP_ARG="--clamp-integers"

run_converter() {
  ssh "$SSH_HOST" "cd '$REMOTE_DIR' && docker compose run --rm -T \
    -v '$REMOTE_TRANSFER_DIR/$SNAP_NAME:/data/import.db:ro' \
    -v '$REMOTE_DIR/ops/import_sqlite.py:/app/ops/import_sqlite.py:ro' \
    web python -m ops.import_sqlite /data/import.db $*"
}

if [ "$CHECK" = "1" ]; then
  echo "==> Contrôle de compatibilité (aucune écriture)"
  run_converter --check-only $CLAMP_ARG
  ssh "$SSH_HOST" "rm -f '$REMOTE_TRANSFER_DIR/$SNAP_NAME'"
  echo "Contrôle terminé."
  exit 0
fi

echo "==> Sauvegarde de l'état actuel (base)"
ssh "$SSH_HOST" "cd '$REMOTE_DIR' && docker compose exec -T db pg_dump -U foyz -d foyz -Fc > '$REMOTE_BACKUP_DIR/pre-import-$TS.dump'"
echo "    -> $REMOTE_BACKUP_DIR/pre-import-$TS.dump"

if [ "$SKIP_UPLOADS" = "0" ]; then
  echo "==> Sauvegarde de l'état actuel (uploads)"
  ssh "$SSH_HOST" "cd '$REMOTE_DIR' && docker compose exec -T web tar -czf - -C /data/uploads . > '$REMOTE_BACKUP_DIR/uploads-pre-import-$TS.tgz'"
  echo "    -> $REMOTE_BACKUP_DIR/uploads-pre-import-$TS.tgz"
fi

echo "==> Import dans la base PostgreSQL de dev"
run_converter --yes $CLAMP_ARG

if [ "$SKIP_UPLOADS" = "0" ]; then
  echo "==> Remplacement des uploads"
  ssh "$SSH_HOST" "cd '$REMOTE_DIR' && docker compose exec -T web find /data/uploads -mindepth 1 -delete"
  tar -czf - -C "$LOCAL_UPLOADS" . \
    | ssh "$SSH_HOST" "cd '$REMOTE_DIR' && docker compose exec -T web tar -xzf - -C /data/uploads"
fi

echo "==> Nettoyage de l'instantané distant"
ssh "$SSH_HOST" "rm -f '$REMOTE_TRANSFER_DIR/$SNAP_NAME'"

echo "==> Contrôle de santé"
ssh "$SSH_HOST" "cd '$REMOTE_DIR' && curl -fsS http://127.0.0.1:8000/health" >/dev/null && echo "  origine OK"
code="$(curl -sS -o /dev/null -w '%{http_code}' "https://$DOMAIN/health" || true)"
[ "$code" = "200" ] && echo "  public OK (HTTP 200)" || echo "  ATTENTION : https://$DOMAIN/health -> ${code:-échec}" >&2

echo
echo "Terminé. Sauvegardes : $REMOTE_BACKUP_DIR (pre-import-$TS.dump, uploads-pre-import-$TS.tgz)"
