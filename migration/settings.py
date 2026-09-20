"""Constantes et réglages du pipeline de migration."""

import os
from datetime import datetime
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

# Durée de conservation des archives de migration (données personnelles des
# anciennes bases) avant destruction : 30 jours par défaut (D14).
ARCHIVE_RETENTION_DAYS = max(1, int(os.environ.get("ARCHIVE_RETENTION_DAYS") or 30))

# Unité par défaut des montants sources : euros (décimaux) convertis en centimes.
DEFAULT_MONEY_UNIT = "euros"
MONEY_UNITS = ("euros", "cents")

# Année du mandat en cours (celle qui prend ses fonctions à la bascule). Le
# dump Brest ne porte aucun marqueur « mandat courant » : `is_foyz` (0/1) vaut
# pour TOUS les anciens membres depuis l'ouverture. On classe donc « mandat »
# les comptes équipe créés l'année précédant le mandat (year(registration) >=
# MANDATE_YEAR - 1), les autres en « ancien ». Ajustable la veille de la
# bascule via MANDATE_YEAR, sinon déduit de l'année courante.
MANDATE_YEAR = int(os.environ.get("MANDATE_YEAR") or datetime.now().year)
