"""Détection automatique des fichiers sources dans bdd_a_migrer/.

Convention de nommage attendue (documentée dans le README §bascule) :
    brest_*.sql                 -> dump MySQL Brest
    paris_*.sql                 -> dump SQL Paris
    paris_*.json / *.jsonl      -> export JSON / JSONL Paris
    paris_*.csv                 -> export CSV Paris

Tout fichier non reconnu (mauvais préfixe, .zip, .gz, …) est signalé en erreur :
la migration refuse de démarrer sur une ambiguïté plutôt que d'ignorer en silence.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from .errors import SourceError
from .settings import DEFAULT_SOURCE_DIR

_KINDS = {
    ".sql": "sql_dump",
    ".json": "json",
    ".jsonl": "jsonl",
    ".ndjson": "jsonl",
    ".csv": "csv",
}

_CAMPUS_PREFIX = {
    "brest": re.compile(r"^brest[-_]", re.IGNORECASE),
    "paris": re.compile(r"^paris[-_]", re.IGNORECASE),
}

_IGNORED_NAMES = {".gitkeep", ".gitignore", "readme.md", "lisez-moi.txt"}


@dataclass(frozen=True)
class SourceFile:
    path: Path
    campus: str  # "brest" | "paris"
    kind: str    # "sql_dump" | "json" | "jsonl" | "csv"

    @property
    def size(self):
        try:
            return self.path.stat().st_size
        except OSError:
            return 0


def scan(source_dir=DEFAULT_SOURCE_DIR, create=True):
    """Scanne le dossier source et retourne la liste des fichiers exploitables.

    Lève SourceError si le dossier est absent/vide ou contient un fichier
    non identifiable. S'assure au passage que le dossier existe (création).
    """
    source_dir = Path(source_dir)
    if not source_dir.exists():
        if create:
            source_dir.mkdir(parents=True, exist_ok=True)
        raise SourceError(
            f"Le dossier source {source_dir} n'existait pas (il vient d'être créé).\n"
            "Déposez-y les dumps : brest_*.sql, paris_*.sql|json|jsonl|csv"
        )
    if not source_dir.is_dir():
        raise SourceError(f"{source_dir} n'est pas un dossier.")

    found, rejected = [], []
    for entry in sorted(source_dir.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if name.lower() in _IGNORED_NAMES or name.startswith("."):
            continue
        campus = next((c for c, rx in _CAMPUS_PREFIX.items() if rx.match(name)), None)
        kind = _KINDS.get(entry.suffix.lower())
        if campus and kind:
            found.append(SourceFile(path=entry, campus=campus, kind=kind))
        else:
            rejected.append(name)

    if rejected:
        raise SourceError(
            "Fichiers non reconnus dans "
            f"{source_dir} (préfixe attendu brest_/paris_, extensions .sql/.json/.jsonl/.csv) : "
            + ", ".join(rejected)
        )
    if not found:
        raise SourceError(
            f"Aucun fichier à migrer dans {source_dir}. "
            "Copiez-y les dumps des deux anciennes bases avant de relancer."
        )
    return found


def group_by_campus(files):
    """Regroupe la liste plate par campus : {"brest": [...], "paris": [...]}."""
    grouped = {"brest": [], "paris": []}
    for f in files:
        grouped[f.campus].append(f)
    return grouped
