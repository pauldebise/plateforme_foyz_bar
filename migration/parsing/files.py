"""Lecteurs des exports Paris : JSON (conteneur), JSONL et CSV.

Tous retournent un itérateur de (table_source, ligne: dict) :
- JSON conteneur {"etudiants": [...], "transactions": [...]} -> chaque clé
  devient une table source ;
- JSON racine liste -> table_source None ;
- JSONL : une ligne = un objet (streaming ligne à ligne) ;
- CSV : DictReader en streaming (séparateur détecté , ou ;) ; l'encodage est
  détecté automatiquement (UTF-8 avec ou sans BOM, sinon CP1252 puis Latin-1 :
  exports de caisse Windows — caisse Paris réelle) et la table source est
  déduite du nom de fichier (paris_clients_1809.csv -> "clients") pour un
  rattachement d'entité explicite, l'inférence par la forme des clés restant
  le filet de sécurité.
"""

import codecs
import csv
import json
import re
from pathlib import Path

from ..errors import SourceError

# Candidats dans l'ordre de priorité : un fichier réellement UTF-8 doit être
# lu comme tel (un accent CP1252 est invalide en UTF-8, la réciproque est
# fausse) ; Latin-1 décode tout octet et sert d'ultime filet.
_CSV_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")


def iter_json(path):
    """Itère un export JSON. Léger par contrat (Paris), chargé en une fois."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SourceError(f"{path.name} : JSON invalide ({exc}).") from None
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                raise SourceError(f"{path.name} : le JSON doit contenir des objets.")
            yield None, row
        return
    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, list):
                for row in value:
                    if not isinstance(row, dict):
                        raise SourceError(
                            f"{path.name} : la section « {key} » doit être une liste d'objets."
                        )
                    yield key, row
        return
    raise SourceError(f"{path.name} : JSON racine inattendu (objet ou liste attendus).")


def iter_jsonl(path):
    """Itère un export JSONL en streaming (une objet JSON par ligne)."""
    with open(path, encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SourceError(f"{path.name} ligne {lineno} : JSON invalide ({exc}).") from None
            if not isinstance(row, dict):
                raise SourceError(f"{path.name} ligne {lineno} : objet JSON attendu.")
            yield None, row


def _decodes(path, encoding):
    """True si le fichier entier se décode dans `encoding` (scan binaire O(1) RAM)."""
    decoder = codecs.getincrementaldecoder(encoding)()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 16):
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError:
                return False
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _sniff_encoding(path):
    """Premier encodage géré qui décode l'intégralité du fichier."""
    for encoding in _CSV_ENCODINGS:
        if _decodes(path, encoding):
            return encoding
    raise SourceError(f"{path.name} : encodage non reconnu (UTF-8 ou CP1252 attendus).")


def table_from_filename(path):
    """Table source déduite du nom de fichier : paris_clients_1809 -> "clients".

    Le préfixe campus et les suffixes numériques (date d'export, version) sont
    retirés ; None si rien ne reste (l'inférence par la forme prend le relais).
    """
    text = re.sub(r"^(brest|paris)[-_]", "", Path(path).stem, flags=re.IGNORECASE)
    text = re.sub(r"[-_.]\d+([-_.]\d+)*$", "", text)
    return text.strip("_-. ") or None


def iter_csv(path):
    """Itère un export CSV en streaming (en-tête = clés).

    Encodage détecté (UTF-8/CP1252/Latin-1) avant toute lecture de ligne :
    un changement d'encodage en cours de fichier est impossible, le scan
    préalable garantit l'absence de redémarrage du générateur.
    """
    encoding = _sniff_encoding(path)
    table = table_from_filename(path)
    with open(path, encoding=encoding, newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        if not reader.fieldnames:
            raise SourceError(f"{path.name} : CSV sans en-tête.")
        for _lineno, row in enumerate(reader, 2):
            clean = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
            if any(v not in (None, "") for v in clean.values()):
                yield table, clean


def iter_rows(path, kind):
    """Dispatch par kind détecté (sql_dump exclu : voir parsing.sqlstream)."""
    if kind == "json":
        return iter_json(path)
    if kind == "jsonl":
        return iter_jsonl(path)
    if kind == "csv":
        return iter_csv(path)
    raise SourceError(f"{path.name} : format {kind} non géré par ce lecteur.")
