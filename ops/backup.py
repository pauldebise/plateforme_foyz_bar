"""Sauvegarde automatique : PostgreSQL + téléversements, rétention et hors-site.

Appelé par cron/systemd (voir README §7). Exemples :

    # Docker Compose (production)
    python -m ops.backup --pg-container foyz-db-1 --pg-user foyz --pg-database foyz \
        --uploads-container foyz-web-1:/data/uploads --backup-dir /srv/backups/foyz

    # Outils PostgreSQL locaux
    DATABASE_URL=postgresql://... python -m ops.backup --backup-dir /srv/backups/foyz

Rétention : `--retention-daily` copies quotidiennes (défaut 7) + une copie
hebdomadaire conservée `--retention-weekly` semaines (défaut 4). L'option
`--offsite` synchronise le dossier vers un hôte de sauvegarde (ssh/rsync).

Le script est idempotent : relancé, il ajoute simplement une nouvelle copie.
Il ne supprime jamais autre chose que ses propres fichiers `foyz-*.dump` /
`uploads-*.tgz` selon la rétention.
"""

import argparse
import os
import re
import shutil
import subprocess
from datetime import datetime, UTC
from pathlib import Path

STAMP_FORMAT = "%Y%m%d-%H%M%S"
_NAME_RE = re.compile(r"^(?P<kind>foyz|uploads)-(?P<stamp>\d{8}-\d{6})\.(?P<ext>dump|tgz)$")
WEEKLY_DIRNAME = "weekly"


def parse_stamp(path: Path):
    match = _NAME_RE.match(Path(path).name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group("stamp"), STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def kind_of(path: Path):
    match = _NAME_RE.match(Path(path).name)
    return match.group("kind") if match else None


def collect(backup_dir: Path):
    """Copies présentes dans le dossier principal, groupées par type."""
    found = {"foyz": [], "uploads": []}
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return found
    for path in backup_dir.iterdir():
        kind = kind_of(path)
        stamp = parse_stamp(path)
        if kind and stamp and path.is_file():
            found[kind].append((stamp, path))
    for kind in found:
        found[kind].sort(key=lambda item: item[0])
    return found


def promote_weekly(backup_dir: Path, log=print):
    """Duplique la dernière copie du jour dans weekly/ (lien dur si possible)."""
    backup_dir = Path(backup_dir)
    weekly = backup_dir / WEEKLY_DIRNAME
    weekly.mkdir(parents=True, exist_ok=True)
    promoted = []
    for _kind, items in collect(backup_dir).items():
        if not items:
            continue
        _, source = items[-1]
        target = weekly / source.name
        if target.exists():
            continue
        try:
            os.link(source, target)
        except OSError:
            shutil.copy2(source, target)
        promoted.append(target)
        log(f"  copie hebdomadaire : {target.name}")
    return promoted


def prune(backup_dir: Path, daily=7, weekly=4, log=print, dry_run=False):
    """Supprime les copies au-delà de la rétention ; retourne les chemins retirés."""
    backup_dir = Path(backup_dir)
    removed = []
    for _kind, items in collect(backup_dir).items():
        keep = items[-daily:] if daily > 0 else []
        for _, path in items[: len(items) - len(keep)]:
            if not dry_run:
                path.unlink()
            removed.append(path)
            log(f"  purgé (quotidien) : {path.name}")
    weekly_dir = backup_dir / WEEKLY_DIRNAME
    weekly_items = sorted(
        (p for p in weekly_dir.iterdir() if parse_stamp(p)) if weekly_dir.is_dir() else [],
        key=parse_stamp,
    )
    keep_weekly = weekly_items[-weekly:] if weekly > 0 else []
    for path in weekly_items[: len(weekly_items) - len(keep_weekly)]:
        if not dry_run:
            path.unlink()
        removed.append(path)
        log(f"  purgé (hebdomadaire) : {path.name}")
    return removed


def is_weekly_day(now):
    return now.weekday() == 6  # dimanche (UTC)


def checked(cmd, **kwargs):
    try:
        return subprocess.run(cmd, check=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Échec de la commande ({exc.returncode}) : {' '.join(cmd)}") from exc
    except FileNotFoundError as exc:
        raise SystemExit(f"Commande introuvable : {cmd[0]}") from exc


def run(cmd, log=print, dry_run=False):
    log("  $ " + " ".join(cmd))
    if dry_run:
        return
    checked(cmd, stdin=subprocess.DEVNULL)


def _docker_exec(container, argv, stdout_file, dry_run):
    cmd = ["docker", "exec", "-i", container, *argv]
    if dry_run:
        print("  $ " + " ".join(cmd) + f" > {stdout_file}")
        return
    try:
        with open(stdout_file, "wb") as out:
            checked(cmd, stdout=out)
    except SystemExit:
        Path(stdout_file).unlink(missing_ok=True)
        raise


def _native(argv, stdout_file, dry_run, env=None):
    if dry_run:
        suffix = f" > {stdout_file}" if stdout_file else ""
        print("  $ " + " ".join(argv) + suffix)
        return
    try:
        if stdout_file is None:
            checked(argv, env=env)
            return
        with open(stdout_file, "wb") as out:
            checked(argv, stdout=out, env=env)
    except SystemExit:
        if stdout_file is not None:
            Path(stdout_file).unlink(missing_ok=True)
        raise


def dump_database(args, stamp, backup_dir, log, dry_run):
    target = backup_dir / f"foyz-{stamp}.dump"
    if args.pg_container:
        _docker_exec(
            args.pg_container,
            ["pg_dump", "-U", args.pg_user, "-d", args.pg_database, "-Fc"],
            target,
            dry_run,
        )
    else:
        if not shutil.which("pg_dump"):
            raise SystemExit(
                "pg_dump introuvable : installez postgresql-client ou utilisez --pg-container."
            )
        if not args.database_url:
            raise SystemExit("DATABASE_URL ou --database-url est requis pour pg_dump.")
        env = {**os.environ, "PGPASSWORD": password_of(args.database_url)}
        _native(["pg_dump", "-Fc", "-d", args.database_url], target, dry_run, env=env)
    return target


def password_of(url):
    match = re.match(r"^[a-z+]+://[^:/@]+:([^@/]+)@", url or "")
    return match.group(1) if match else os.environ.get("PGPASSWORD", "")


def archive_uploads(args, stamp, backup_dir, log, dry_run):
    target = backup_dir / f"uploads-{stamp}.tgz"
    if args.uploads_container:
        container, _, path = args.uploads_container.partition(":")
        _docker_exec(
            container,
            ["tar", "-czf", "-", "-C", path or "/data/uploads", "."],
            target,
            dry_run,
        )
    else:
        source = Path(args.uploads_dir)
        if not source.is_dir():
            raise SystemExit(f"Dossier d'uploads introuvable : {source}")
        _native(["tar", "-czf", str(target), "-C", str(source), "."], None, dry_run)
    return target


def offsite_sync(backup_dir, target, log, dry_run):
    if not target:
        return
    if not shutil.which("rsync"):
        raise SystemExit("rsync introuvable : impossible d'envoyer hors-site.")
    run(
        ["rsync", "-a", "--delete", f"{backup_dir}/", target],
        log=log,
        dry_run=dry_run,
    )


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m ops.backup",
        description="Sauvegarde de la base et des téléversements (rétention + hors-site).",
    )
    parser.add_argument(
        "--backup-dir", type=Path, default=Path(os.environ.get("BACKUP_DIR", "backups"))
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--pg-container", default=os.environ.get("BACKUP_PG_CONTAINER"))
    parser.add_argument("--pg-user", default=os.environ.get("BACKUP_PG_USER", "foyz"))
    parser.add_argument("--pg-database", default=os.environ.get("BACKUP_PG_DATABASE", "foyz"))
    parser.add_argument(
        "--uploads-dir", type=Path, default=Path(os.environ.get("UPLOAD_DIR", "instance/uploads"))
    )
    parser.add_argument("--uploads-container", default=os.environ.get("BACKUP_UPLOADS_CONTAINER"))
    parser.add_argument("--retention-daily", type=int, default=7)
    parser.add_argument("--retention-weekly", type=int, default=4)
    parser.add_argument("--offsite", default=os.environ.get("BACKUP_OFFSITE"))
    parser.add_argument(
        "--prune-only", action="store_true", help="applique uniquement la rétention (aucun dump)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="affiche les commandes sans rien exécuter"
    )
    args = parser.parse_args(argv)
    if args.retention_daily < 1 or args.retention_weekly < 0:
        parser.error("rétentions invalides (quotidienne >= 1, hebdomadaire >= 0)")
    args.backup_dir = Path(args.backup_dir)
    return args


def main(argv=None):
    args = parse_args(argv)
    log = print
    now = datetime.now(UTC)
    stamp = now.strftime(STAMP_FORMAT)
    args.backup_dir.mkdir(parents=True, exist_ok=True)

    if args.prune_only:
        log(f"=== Rétention seule ({args.backup_dir}) ===")
        prune(
            args.backup_dir,
            args.retention_daily,
            args.retention_weekly,
            log=log,
            dry_run=args.dry_run,
        )
        offsite_sync(args.backup_dir, args.offsite, log, args.dry_run)
        return 0

    log(f"=== Sauvegarde {stamp} -> {args.backup_dir} ===")
    db_file = dump_database(args, stamp, args.backup_dir, log, args.dry_run)
    up_file = archive_uploads(args, stamp, args.backup_dir, log, args.dry_run)
    if is_weekly_day(now) and not args.dry_run:
        promote_weekly(args.backup_dir, log=log)
    prune(
        args.backup_dir, args.retention_daily, args.retention_weekly, log=log, dry_run=args.dry_run
    )
    offsite_sync(args.backup_dir, args.offsite, log, args.dry_run)
    if not args.dry_run:
        log(
            f"OK : {db_file.name} ({db_file.stat().st_size} o), {up_file.name} ({up_file.stat().st_size} o)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
