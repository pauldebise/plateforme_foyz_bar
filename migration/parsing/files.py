"""Lecteurs des exports Paris : JSON (conteneur), JSONL et CSV.

Tous retournent un itérateur de (table_source, ligne: dict) :
- JSON conteneur {"etudiants": [...], "transactions": [...]} -> chaque clé
  devient une table source ;
- JSON racine liste -> table_source None ;
- JSONL : une ligne = un objet (streaming ligne à ligne) ;
- CSV : DictReader en streaming (séparateur détecté , ou ;).
"""

import csv
import json

from ..errors import SourceError


def iter_json(path):
    """Itère un export JSON. Léger par contrat (Paris), chargé en une fois."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
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
    with open(path, "r", encoding="utf-8") as fh:
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


def iter_csv(path):
    """Itère un export CSV en streaming (en-tête = clés)."""
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(fh, dialect=dialect)
        if not reader.fieldnames:
            raise SourceError(f"{path.name} : CSV sans en-tête.")
        for lineno, row in enumerate(reader, 2):
            clean = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items() if k}
            if any(v not in (None, "") for v in clean.values()):
                yield None, clean


def iter_rows(path, kind):
    """Dispatch par kind détecté (sql_dump exclu : voir parsing.sqlstream)."""
    if kind == "json":
        return iter_json(path)
    if kind == "jsonl":
        return iter_jsonl(path)
    if kind == "csv":
        return iter_csv(path)
    raise SourceError(f"{path.name} : format {kind} non géré par ce lecteur.")
