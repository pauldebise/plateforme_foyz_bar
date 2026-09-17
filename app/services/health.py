"""État de la plateforme : sonde `/health` et tableau de bord (P10).

Regroupe les contrôles communs (base, téléversements, schéma), l'espace disque
et l'inventaire des sauvegardes. La route `/health` et la page
d'administration `/admin/sante` s'appuient sur ce module pour ne jamais
diverger.
"""

import os
import shutil
from datetime import datetime, UTC
from pathlib import Path

from flask import current_app

from app.extensions import db


def database_ok():
    from sqlalchemy import text

    try:
        with db.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def uploads_ok():
    uploads = Path(current_app.config["UPLOAD_FOLDER"])
    return uploads.is_dir() and os.access(uploads, os.W_OK)


def schema_state():
    from app.schema import current_revision, head_revision, is_sqlite, tables_present

    try:
        current = current_revision()
        head = head_revision()
    except Exception:
        return {
            "current": None,
            "head": None,
            "up_to_date": False,
            "unknown": True,
            "unversioned": False,
        }
    # Base SQLite locale créée par create_all() : schéma complet mais sans
    # table alembic_version — c'est le fonctionnement attendu (voir README 6.1).
    unversioned = bool(current is None and is_sqlite() and tables_present())
    return {
        "current": current,
        "head": head,
        "up_to_date": current == head or unversioned,
        "unknown": False,
        "unversioned": unversioned,
    }


def system_checks():
    """Mêmes indications que `/health`, sans transport HTTP."""
    schema = schema_state()
    checks = {
        "database": "ok" if database_ok() else "error",
        "uploads": "ok" if uploads_ok() else "error",
        "schema": "ok" if schema["up_to_date"] else f"outdated:{schema['current'] or 'none'}",
    }
    return checks


def disk_info(path):
    try:
        usage = shutil.disk_usage(Path(path))
    except OSError:
        return None
    percent = round(usage.used / usage.total * 100) if usage.total else 0
    return {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "percent": percent,
    }


def database_size():
    """Taille du fichier SQLite, ou None (PostgreSQL : voir pg_database_size)."""
    from sqlalchemy.engine import make_url

    try:
        url = make_url(current_app.config["SQLALCHEMY_DATABASE_URI"])
    except Exception:
        return None
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    path = Path(url.database)
    return path.stat().st_size if path.is_file() else None


def backups_info(limit=5):
    """Dernières sauvegardes (quotidiennes et hebdomadaires), chiffrement inclus."""
    from ops import backup as B

    directory = Path(current_app.config["BACKUP_DIR"])
    entries = []
    for location in (directory, directory / B.WEEKLY_DIRNAME):
        found = B.collect(location)
        for kind, items in found.items():
            for _stamp, path in items:
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entries.append(
                    {
                        "kind": kind,
                        "name": path.name,
                        "encrypted": B.is_encrypted(path),
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime, UTC).replace(tzinfo=None),
                        "weekly": location.name == B.WEEKLY_DIRNAME,
                    }
                )
    entries.sort(key=lambda entry: (entry["modified"], entry["name"]), reverse=True)
    return {
        "directory": str(directory),
        "exists": directory.is_dir(),
        "count": len(entries),
        "total_size": sum(entry["size"] for entry in entries),
        "entries": entries[:limit],
    }
