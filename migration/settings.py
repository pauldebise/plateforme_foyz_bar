"""Constantes et réglages du pipeline de migration."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Dossier déposé par les ops contenant les dumps des anciennes bases.
DEFAULT_SOURCE_DIR = PROJECT_ROOT / "bdd_a_migrer"

# Taille d'un batch de lignes lues / injectées (RAM maîtrisée sur les ~500 Mo de Brest).
DEFAULT_CHUNK_ROWS = 2000
MIN_CHUNK_ROWS = 100
MAX_CHUNK_ROWS = 20000

# Taille (caractères) des lectures séquentielles d'un dump SQL : aucun statement
# n'est chargé intégralement en mémoire, même une ligne INSERT étendue de 100 Mo.
SQL_READ_CHUNK = 1 << 20  # 1 MiB

# Table de chargement intermédiaire pour les exports Paris hétérogènes.
STAGING_TABLE = "staging_paris_raw"

# Nom du sous-dossier d'archivage (--keep-archives), horodaté à l'exécution.
ARCHIVE_DIRNAME = "archives"

# Unité par défaut des montants sources : euros (décimaux) convertis en centimes.
DEFAULT_MONEY_UNIT = "euros"
MONEY_UNITS = ("euros", "cents")
