"""Restauration d'une sauvegarde sur un environnement vierge.

    # Lister les sauvegardes disponibles
    python -m ops.restore --list --backup-dir /srv/backups/foyz

    # Restaurer la dernière paire (base + uploads)
    python -m ops.restore --latest --backup-dir /srv/backups/foyz \
        --pg-container foyz-db-1 --pg-user foyz --pg-database foyz \
        --uploads-container foyz-web-1:/data/uploads --yes

La base cible doit exister (créée par `flask --app wsgi.py init-db`) : la
restauration remplace son contenu (`pg_restore --clean --if-exists`), elle ne
crée pas le rôle PostgreSQL. Voir la procédure complète dans README §7.

Garde-fou : sans `--yes`, rien n'est exécuté (une restauration écrase la
cible). `--dry-run` affiche les commandes.
"""

import argparse
import os
import shutil
import subprocess
from pathlib import Path

from ops.backup import (
    STAMP_FORMAT,
    check_passphrase_file,
    collect,
    decrypt_to_temp,
    is_encrypted,
    password_of,
    parse_stamp,
)

WEEKLY_DIRNAME = "weekly"


def pairs(backup_dir: Path):
    """Paires (horodatage, dump, uploads) du dossier principal puis weekly/."""
    result = {}
    for directory in (Path(backup_dir), Path(backup_dir) / WEEKLY_DIRNAME):
        found = collect(directory)
        for stamp, dump in found["foyz"]:
            result.setdefault(stamp, {"dump": dump, "uploads": None})
        for stamp, archive in found["uploads"]:
            if stamp in result:
                result[stamp]["uploads"] = archive
    return [(stamp, entry["dump"], entry["uploads"]) for stamp, entry in sorted(result.items())]


def list_backups(backup_dir: Path, log=print):
    entries = pairs(backup_dir)
    if not entries:
        log(f"Aucune sauvegarde dans {backup_dir}")
        return []
    for stamp, dump, archive in entries:
        archive_name = archive.name if archive else "-"
        marker = " [chiffrée]" if is_encrypted(dump) else ""
        log(f"{stamp.strftime(STAMP_FORMAT)}  {dump.name:32} {archive_name}{marker}")
    return [stamp for stamp, _, _ in entries]


def resolve_pair(backup_dir: Path, database=None, uploads=None):
    entries = pairs(backup_dir)
    if database:
        dump = Path(database)
        archive = Path(uploads) if uploads else None
        if archive is None:
            stamp = parse_stamp(dump)
            archive = next((a for s, _, a in entries if s == stamp), None)
    else:
        if not entries:
            raise SystemExit(f"Aucune sauvegarde dans {backup_dir}")
        _, dump, archive = entries[-1]
        if uploads:
            archive = Path(uploads)
    if not Path(dump).exists():
        raise SystemExit(f"Dump introuvable : {dump}")
    if archive is not None and not Path(archive).exists():
        raise SystemExit(f"Archive d'uploads introuvable : {archive}")
    return Path(dump), (Path(archive) if archive else None)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m ops.restore",
        description="Restaure la base et les téléversements depuis une sauvegarde.",
    )
    parser.add_argument(
        "--backup-dir", type=Path, default=Path(os.environ.get("BACKUP_DIR", "backups"))
    )
    parser.add_argument(
        "--database", type=Path, default=None, help="fichier .dump précis (défaut : --latest)"
    )
    parser.add_argument(
        "--uploads",
        type=Path,
        default=None,
        help="archive .tgz précise (défaut : même horodatage que le dump)",
    )
    parser.add_argument("--latest", action="store_true", help="prend la sauvegarde la plus récente")
    parser.add_argument("--list", action="store_true", help="liste les sauvegardes")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--pg-container", default=os.environ.get("BACKUP_PG_CONTAINER"))
    parser.add_argument("--pg-user", default=os.environ.get("BACKUP_PG_USER", "foyz"))
    parser.add_argument("--pg-database", default=os.environ.get("BACKUP_PG_DATABASE", "foyz"))
    parser.add_argument(
        "--uploads-dir", type=Path, default=Path(os.environ.get("UPLOAD_DIR", "instance/uploads"))
    )
    parser.add_argument("--uploads-container", default=os.environ.get("BACKUP_UPLOADS_CONTAINER"))
    parser.add_argument(
        "--passphrase-file",
        type=Path,
        default=(
            Path(os.environ["BACKUP_PASSPHRASE_FILE"])
            if os.environ.get("BACKUP_PASSPHRASE_FILE")
            else None
        ),
        help="phrase de passe des sauvegardes chiffrées (GnuPG)",
    )
    parser.add_argument("--yes", action="store_true", help="confirme l'écrasement de la cible")
    parser.add_argument("--dry-run", action="store_true", help="affiche sans exécuter")
    args = parser.parse_args(argv)
    if not args.list and not args.database and not args.latest:
        parser.error("indiquez --database <fichier>, --latest ou --list.")
    if not args.list and not (args.pg_container or args.database_url):
        parser.error("indiquez --pg-container ou --database-url.")
    if args.passphrase_file is not None:
        args.passphrase_file = check_passphrase_file(args.passphrase_file)
    return args


def checked(cmd, **kwargs):
    try:
        return subprocess.run(cmd, check=True, **kwargs)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Échec de la commande ({exc.returncode}) : {' '.join(cmd)}") from exc
    except FileNotFoundError as exc:
        raise SystemExit(f"Commande introuvable : {cmd[0]}") from exc


def run(cmd, log=print, dry_run=False, stdin=None):
    log("  $ " + " ".join(cmd))
    if dry_run:
        return
    checked(cmd, stdin=stdin)


def source_for_restore(args, path, log, dry_run):
    """Chemin lisible (déchiffré au besoin) et temporaire à nettoyer ensuite."""
    path = Path(path)
    if not is_encrypted(path):
        return path, None
    if not args.passphrase_file:
        raise SystemExit(
            f"{path.name} est chiffrée : indiquez --passphrase-file (ou BACKUP_PASSPHRASE_FILE)."
        )
    if dry_run:
        log(f"  $ gpg --decrypt {path.name} > (fichier temporaire)")
        return path, None
    return decrypt_to_temp(path, args.passphrase_file, log=log), True


def restore_database(args, dump, log, dry_run):
    source, temp = source_for_restore(args, dump, log, dry_run)
    try:
        if args.pg_container:
            cmd = [
                "docker",
                "exec",
                "-i",
                args.pg_container,
                "pg_restore",
                "--clean",
                "--if-exists",
                "--no-owner",
                "-U",
                args.pg_user,
                "-d",
                args.pg_database,
            ]
            log("  $ " + " ".join(cmd) + f" < {dump}")
            if dry_run:
                return
            with open(source, "rb") as handle:
                checked(cmd, stdin=handle)
            return
        if not shutil.which("pg_restore"):
            raise SystemExit("pg_restore introuvable : installez postgresql-client.")
        env = {**os.environ, "PGPASSWORD": password_of(args.database_url)}
        cmd = [
            "pg_restore",
            "--clean",
            "--if-exists",
            "--no-owner",
            "-d",
            args.database_url,
            str(source),
        ]
        log("  $ pg_restore ... " + dump.name)
        if not dry_run:
            checked(cmd, env=env)
    finally:
        if temp:
            Path(source).unlink(missing_ok=True)


def restore_uploads(args, archive, log, dry_run):
    if archive is None:
        log("  (aucune archive d'uploads : ignorée)")
        return
    source, temp = source_for_restore(args, archive, log, dry_run)
    try:
        if args.uploads_container:
            container, _, path = args.uploads_container.partition(":")
            cmd = [
                "docker",
                "exec",
                "-i",
                container,
                "tar",
                "-xzf",
                "-",
                "-C",
                path or "/data/uploads",
            ]
            log("  $ " + " ".join(cmd) + f" < {archive}")
            if dry_run:
                return
            with open(source, "rb") as handle:
                checked(cmd, stdin=handle)
            return
        target = Path(args.uploads_dir)
        target.mkdir(parents=True, exist_ok=True)
        run(["tar", "-xzf", str(source), "-C", str(target)], log=log, dry_run=dry_run)
    finally:
        if temp:
            Path(source).unlink(missing_ok=True)


def main(argv=None):
    args = parse_args(argv)
    log = print
    if args.list:
        list_backups(args.backup_dir, log=log)
        return 0
    dump, archive = resolve_pair(args.backup_dir, args.database, args.uploads)
    log(f"=== Restauration {dump.name} ===")
    if not args.yes and not args.dry_run:
        log("Refusé : ajoutez --yes (la restauration écrase la cible).")
        return 1
    restore_database(args, dump, log, args.dry_run)
    restore_uploads(args, archive, log, args.dry_run)
    log("Restauration terminée — vérifiez les totaux (voir README §7).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
