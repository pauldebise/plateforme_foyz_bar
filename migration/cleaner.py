"""Nettoyage sécurisé du dossier bdd_a_migrer/ après COMMIT validé.

Règle absolue : aucun fichier n'est touché avant la confirmation du COMMIT
(la CLI n'appelle ces fonctions qu'après). Seuls les fichiers listés dans le
manifest détecté sont supprimés ou archivés — jamais d'autre contenu.
"""

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .errors import SourceError
from .report import kv, line, section
from .settings import ARCHIVE_DIRNAME


def dispose(source_files, keep_archives=False, source_dir=None, consumed=None):
    """Supprime (ou archive) les fichiers sources traités.

    - keep_archives=False : suppression définitive fichier par fichier ;
    - keep_archives=True  : déplacement vers <source_dir>/archives/<horodatage>/.
    `consumed` : noms de fichiers réellement lus ; tout fichier non consommé
    fait échouer le nettoyage AVANT la moindre suppression (R7).
    Retourne le chemin d'archivage éventuel. Lève une exception si un fichier
    n'a pas pu être retiré du dossier (l'opération est vérifiée).
    """
    section("NETTOYAGE DES FICHIERS SOURCES")
    if consumed is not None:
        refused = [src.path.name for src in source_files if src.path.name not in consumed]
        if refused:
            raise SourceError(
                "Nettoyage refusé : fichiers jamais consommés : " + ", ".join(refused)
            )
    if keep_archives:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        archive_dir = Path(source_dir or Path(source_files[0].path).parent) / ARCHIVE_DIRNAME / stamp
        archive_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for src in source_files:
        path = Path(src.path)
        if not path.exists():
            kv(f"Déjà absent", path.name)
            continue
        if keep_archives:
            target = archive_dir / path.name
            shutil.move(str(path), str(target))
            if not path.exists() and target.exists():
                kv("Archivé", f"{path.name} -> {target}")
                moved += 1
            else:
                raise RuntimeError(f"Échec d'archivage : {path}")
        else:
            path.unlink()
            if not path.exists():
                kv("Supprimé", path.name)
                moved += 1
            else:
                raise RuntimeError(f"Échec de suppression : {path}")
    line()
    if keep_archives:
        print(f"  {moved} fichier(s) archivé(s) dans {archive_dir}")
    else:
        print(f"  {moved} fichier(s) source(s) supprimé(s) — bdd_a_migrer/ nettoyé.")
    line()
    return archive_dir if keep_archives else None


def purge_archives(source_dir, days, dry_run=False, now=None, log=print):
    """Détruit les archives de migration plus anciennes que `days` jours (D14).

    Les archives contiennent des données personnelles (anciennes bases) : elles
    ne doivent pas rester indéfiniment. Seuls les sous-dossiers horodatés
    `AAAAMMJJ-HHMMSS` sont concernés ; tout autre contenu est ignoré.
    """
    archives_dir = Path(source_dir) / ARCHIVE_DIRNAME
    if not archives_dir.is_dir():
        log(f"  Aucune archive dans {archives_dir}")
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    removed = []
    for entry in sorted(archives_dir.iterdir()):
        if not entry.is_dir():
            continue
        try:
            stamp = datetime.strptime(entry.name, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if stamp >= cutoff:
            continue
        size = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
        if dry_run:
            log(f"  [simulation] {entry.name} ({size // 1024} KiB)")
        else:
            shutil.rmtree(entry)
            log(f"  Détruit : {entry.name} ({size // 1024} KiB)")
        removed.append(str(entry))
    log(f"  {len(removed)} archive(s) de plus de {days} jour(s).")
    return removed
