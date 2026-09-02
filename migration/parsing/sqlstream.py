"""Parseur streaming de dumps SQL (mysqldump / exports SQL génériques).

Contrainte RAM : les dumps peuvent contenir des INSERT étendus de plusieurs
dizaines de Mo (les ~500 Mo de Brest sont majoritairement des logs). Aucun
statement n'est donc chargé intégralement : lecture par blocs de 1 MiB,
machine à états (chaînes, échappements, commentaires), et les statements des
tables exclues par la liste blanche sont *consommés sans être mémorisés*.

Le parseur fournit :
- SqlDumpScanner : itérateur de statements (kind, table, texte) — texte None
  pour les statements ignorés — et des statistiques (tables vues, statements
  et ~lignes de logs ignorés) ;
- iter_business_rows() : itérateur par batchs de lignes dict pour les tables
  métier (colonnes déduites du CREATE TABLE ou de la liste de colonnes).
- Limitation assumée : triggers/stored procedures non supportés (inutiles ici).
"""

import re
from collections import defaultdict
from pathlib import Path

from ..errors import SourceError
from .. import settings

_TOP_LEVEL = re.compile(r"['\"`;]")
_IN_STRING = re.compile(r"['\"\\]")
_DECIDE_INSERT = re.compile(
    r"^\s*(?:INSERT\s+(?:IGNORE\s+)?|REPLACE\s+(?:IGNORE\s+)?)INTO\s+"
    r"(?:`?[\w$]+`?\.)?`?([\w$]+)`?",
    re.IGNORECASE,
)
_DECIDE_CREATE = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:`?[\w$]+`?\.)?`?([\w$]+)`?",
    re.IGNORECASE,
)
_DECIDE_START = re.compile(r"^\s*(INSERT|CREATE|REPLACE)\b", re.IGNORECASE)
_SIGNIFICANT = re.compile(r"[^ \t\r\n;]+")
_COL_DEF = re.compile(r"^\s*`?(\w+)`?\s+([A-Za-z]+)")
_KEYWORD_LINES = {
    "PRIMARY", "UNIQUE", "KEY", "INDEX", "FULLTEXT", "SPATIAL", "CONSTRAINT",
    "FOREIGN", "CHECK",
}
_HEADER_INSERT = re.compile(
    r"^\s*(?:INSERT\s+(?:IGNORE\s+)?|REPLACE\s+(?:IGNORE\s+)?)INTO\s+"
    r"(?:`?[\w$]+`?\.)?`?[\w$]+`?\s*(\(([^)]*)\))?\s*VALUES?\s*",
    re.IGNORECASE | re.DOTALL,
)
_INT_RE = re.compile(r"^[+-]?\d+$")

# longueur max du préfixe analysé pour décider du sort d'un statement
_DECIDE_MAX = 256


class SqlDumpScanner:
    """Itère les statements d'un dump SQL en streaming avec statistiques."""

    def __init__(self, fh, keep_predicate=None, chunk_size=settings.SQL_READ_CHUNK):
        self.fh = fh
        self.keep_predicate = keep_predicate or (lambda table: False)
        self.chunk_size = chunk_size
        # Statistiques remplie pendant le scan (consommées par le rapport).
        self.tables_seen = set()
        self.tables_created = {}                    # table -> [colonnes] (métier)
        self.insert_statements = defaultdict(int)   # table -> nb statements (métier)
        self.skipped_statements = defaultdict(int)  # table/clé -> nb statements ignorés
        self.skipped_rows_approx = defaultdict(int) # table/clé -> ~ lignes ignorées

    # ------------------------------------------------------------------ scan

    def statements(self):
        """Générateur (kind, table, texte) ; texte None si statement ignoré.

        kind : "create" (structure d'une table métier), "insert" (données d'une
        table métier), "other" (statement technique : jamais mémorisé).
        """
        state = "start"      # start | decide | keep | skip | comment_line | comment_block
        table = kind = None
        str_q = None         # quote ouvrant de la chaîne courante (' ou ")
        escape = False
        ident = False        # dans un identifiant `backticks`
        decide_buf = []
        keep_buf = []

        while True:
            chunk = self.fh.read(self.chunk_size)
            if not chunk:
                break
            pos, n = 0, len(chunk)

            while pos < n:
                # ---- à l'intérieur d'une chaîne ' ou "
                if str_q is not None:
                    pos, str_q, escape = self._scan_string(
                        chunk, pos, str_q, escape, keep_buf if state == "keep" else None
                    )
                    continue

                # ---- à l'intérieur d'un identifiant `backticks`
                if ident:
                    pos, closed = self._scan_ident(
                        chunk, pos, keep_buf if state == "keep" else None
                    )
                    if closed:
                        ident = False
                    continue

                if state == "comment_line":
                    nl = chunk.find("\n", pos)
                    pos = (nl + 1) if nl != -1 else n
                    if nl != -1:
                        state = "start"
                    continue
                if state == "comment_block":
                    end = chunk.find("*/", pos)
                    pos = (end + 2) if end != -1 else n
                    if end != -1:
                        state = "start"
                    continue

                if state in ("start", "decide"):
                    m = _SIGNIFICANT.search(chunk, pos)
                    if not m:
                        pos = n
                        break
                    ch = chunk[m.start()]
                    if state == "start":
                        if ch == "-" and chunk.startswith("--", m.start()):
                            state = "comment_line"
                            pos = m.start()
                            continue
                        if ch == "#":
                            state = "comment_line"
                            pos = m.start()
                            continue
                        if ch == "/" and chunk.startswith("/*", m.start()):
                            state = "comment_block"
                            pos = m.start()
                            continue
                        state = "decide"
                        decide_buf = []
                    # state == decide : on accumule un préfixe borné
                    take = chunk[m.start():m.start() + _DECIDE_MAX]
                    decide_buf.append(take)
                    buf = "".join(decide_buf)
                    mi, mc = _DECIDE_INSERT.match(buf), _DECIDE_CREATE.match(buf)
                    if mi or mc:
                        # décision : on rembobine juste après l'en-tête pour que la
                        # suite du statement (y compris un éventuel `;` immédiat)
                        # soit correctement traitée par les états keep/skip.
                        header_end = (mi or mc).end()
                        table = (mi or mc).group(1)
                        kind = "insert" if mi else "create"
                        self.tables_seen.add(table.lower())
                        pos = m.start() + header_end
                        if self.keep_predicate(table):
                            state = "keep"
                            keep_buf = [buf[:header_end]]
                        else:
                            state = "skip"
                            decide_buf = []
                            if kind == "insert":
                                self.skipped_rows_approx[table] += 1
                        continue
                    if not _DECIDE_START.match(buf) or len(buf) >= _DECIDE_MAX:
                        # statement technique (SET, LOCK, DROP…) : consommé sans mémoire
                        kind = "other"
                        table = None
                        state = "skip"
                        decide_buf = []
                        pos = m.start()
                        continue
                    pos = m.start() + len(take)
                    continue  # besoin de plus de caractères pour décider

                # ---- state keep / skip : délimiteurs top level
                m = _TOP_LEVEL.search(chunk, pos)
                if state == "keep":
                    if not m:
                        keep_buf.append(chunk[pos:])
                        pos = n
                        break
                    keep_buf.append(chunk[pos:m.end()])
                elif state == "skip":
                    if kind == "insert" and table:
                        stop = m.start() if m else n
                        self.skipped_rows_approx[table] += chunk[pos:stop].count("),(")
                    if not m:
                        pos = n
                        break
                else:  # pragma: no cover - état impossible
                    raise RuntimeError(f"état inattendu : {state}")
                pos = m.end() if m else n
                if not m:
                    break
                ch = m.group()
                if ch in ("'", '"'):
                    str_q, escape = ch, False
                elif ch == "`":
                    ident = True
                elif ch == ";":
                    if state == "keep":
                        text = "".join(keep_buf).rstrip()
                        if text.endswith(";"):
                            text = text[:-1]
                        if text.strip():
                            yield (kind, table, text)
                        keep_buf = []
                    elif state == "skip":
                        key = table if kind in ("insert", "create") else f"__{kind}"
                        self.skipped_statements[key] += 1
                    state, table, kind = "start", None, None
                    decide_buf = []

            # fin de chunk : l'état (state, str_q, escape, buffers) porte tout
        # fin de fichier
        if state == "keep" and keep_buf:
            text = "".join(keep_buf).rstrip()
            if text.endswith(";"):
                text = text[:-1]
            if text.strip():
                yield (kind, table, text)

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _scan_string(chunk, pos, quote, escape, sink):
        """Avance dans une chaîne ; retourne (nouvelle_pos, quote_restant, escape).

        quote_restant vaut None une fois la chaîne fermée.
        sink : liste à alimenter (mode keep), None pour jeter le contenu.
        """
        n = len(chunk)
        while True:
            m = _IN_STRING.search(chunk, pos)
            if not m:
                if sink is not None:
                    sink.append(chunk[pos:])
                return n, quote, escape
            ch = m.group()
            if sink is not None:
                sink.append(chunk[pos:m.end()])
            pos = m.end()
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                return pos, None, escape

    @staticmethod
    def _scan_ident(chunk, pos, sink):
        """Avance dans un identifiant `backticks` (`` doublé = littéral).

        Retourne (nouvelle_pos, fermé_bool).
        """
        while True:
            close = chunk.find("`", pos)
            if close == -1:
                if sink is not None:
                    sink.append(chunk[pos:])
                return len(chunk), False
            if sink is not None:
                sink.append(chunk[pos:close + 1])
            doubled = close + 1 < len(chunk) and chunk[close + 1] == "`"
            if doubled:
                if sink is not None:
                    sink.append("`")
                pos = close + 2
                continue
            return close + 1, True

    def columns_of(self, statement_text):
        """Extrait la liste ordonnée des colonnes d'un CREATE TABLE.

        Analyse la section entre les parenthèses externes (multi-ligne ou
        mono-ligne), en respectant chaînes, backticks et parenthèses imbriquées
        (ENUM, DECIMAL(10,2)…). Les définitions de clés/index sont écartées.
        """
        m = re.match(r"^\s*CREATE\s+TABLE\s+.*?\(", statement_text, re.IGNORECASE | re.DOTALL)
        if not m:
            return []
        body, _ = self._balanced_span(statement_text, m.end() - 1)
        if body is None:
            return []
        columns = []
        for segment in self._split_top_level(body):
            seg = segment.strip()
            cm = re.match(r"^`?(\w+)`?\s+([A-Za-z]+)", seg)
            if not cm:
                continue
            first, second = cm.group(1), cm.group(2)
            if first.upper() in _KEYWORD_LINES or second.upper() in ("KEY", "INDEX"):
                continue
            columns.append(first)
        return columns

    @staticmethod
    def _balanced_span(text, open_pos):
        """Retourne (contenu, index_apres_fermeture) de la parenthèse ouverte en
        open_pos, en respectant chaînes et backticks. (None, open_pos) si non fermée."""
        depth, i, n = 0, open_pos, len(text)
        quote = None
        escape = False
        while i < n:
            ch = text[i]
            if quote:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "`":
                close = text.find("`", i + 1)
                if close == -1:
                    return None, open_pos
                i = close
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return text[open_pos + 1:i], i + 1
            i += 1
        return None, open_pos

    @staticmethod
    def _split_top_level(body):
        """Découpe sur les virgules de premier niveau (parenthèses/chaînes respectées)."""
        segments = []
        start = 0
        depth = 0
        quote = None
        escape = False
        for i, ch in enumerate(body):
            if quote:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                segments.append(body[start:i])
                start = i + 1
        segments.append(body[start:])
        return segments


def _looks_terminated(buf):
    """(conservé pour lisibilité des tests) True si le buffer n'est pas INSERT/CREATE."""
    head = buf.lstrip()[:12].upper()
    return not (head.startswith("INSERT") or head.startswith("CREATE") or head.startswith("REPLACE"))


def iter_business_rows(path, keep_map, batch_size=settings.DEFAULT_CHUNK_ROWS, encoding=None):
    """Itère (table, colonnes, lignes[dict]) par batchs pour les tables métier.

    keep_map : dict table_minuscules -> identifiant logique (liste blanche).
    Colonnes issues du CREATE TABLE du dump, ou de la liste explicite de
    colonnes de l'INSERT ; sinon SourceError.
    """
    path = Path(path)
    enc = encoding or _sniff_encoding(path)
    with open(path, "r", encoding=enc, newline="") as fh:
        scanner = SqlDumpScanner(fh, keep_predicate=lambda t: bool(t) and t.lower() in keep_map)
        pending_table, pending_cols, batch = None, None, []
        for kind, table, text in scanner.statements():
            if kind == "create" and text:
                scanner.tables_created[table.lower()] = scanner.columns_of(text)
                continue
            if kind != "insert" or not text:
                continue
            scanner.insert_statements[table.lower()] += 1
            columns = _explicit_columns(text) or scanner.tables_created.get(table.lower())
            if not columns:
                raise SourceError(
                    f"{path.name} : impossible de déterminer les colonnes de la table "
                    f"`{table}` (ni CREATE TABLE lu, ni liste de colonnes dans l'INSERT)."
                )
            for row in _parse_values(text, columns, table, path.name):
                if pending_table != table:
                    if batch:
                        yield pending_table, pending_cols, batch
                        batch = []
                    pending_table, pending_cols = table, columns
                batch.append(row)
                if len(batch) >= batch_size:
                    yield pending_table, pending_cols, batch
                    batch = []
        if batch:
            yield pending_table, pending_cols, batch


def _explicit_columns(statement):
    m = _HEADER_INSERT.match(statement)
    if m and m.group(2):
        return [c.strip().strip("`") for c in m.group(2).split(",") if c.strip()]
    return None


def _sniff_encoding(path):
    """utf-8 si possible, sinon latin-1 avec avertissement (dumps anciens)."""
    with open(path, "rb") as fh:
        head = fh.read(65536)
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        print(f"[migration] AVERTISSEMENT : {path.name} n'est pas de l'utf-8, lecture en latin-1.")
        return "latin-1"


class _ValueParser:
    """Tokeniseur des tuples VALUES d'un INSERT (échappements MySQL gérés)."""

    _UNESCAPE = {
        "0": "\0", "'": "'", '"': '"', "b": "\b", "n": "\n", "r": "\r",
        "t": "\t", "Z": "\x1a", "\\": "\\",
    }
    _QUOTE_OR_BACKSLASH = re.compile(r"['\"\\]")

    def __init__(self, text):
        self.text = text
        self.n = len(text)

    def skip_ws(self, i):
        while i < self.n and self.text[i] in " \t\r\n":
            i += 1
        return i

    def parse_string(self, i):
        quote = self.text[i]
        i += 1
        out = []
        while i < self.n:
            m = self._QUOTE_OR_BACKSLASH.search(self.text, i)
            if not m:
                raise ValueError("chaîne non terminée dans l'INSERT")
            ch = m.group()
            out.append(self.text[i:m.start()])
            i = m.end()
            if ch == "\\":
                nxt = self.text[i] if i < self.n else ""
                out.append(self._UNESCAPE.get(nxt, nxt))
                i += 1
            elif ch == quote:
                return "".join(out), i
            else:
                out.append(ch)
        raise ValueError("chaîne non terminée dans l'INSERT")


def _parse_values(statement, columns, table, filename):
    """Générateur de dicts {colonne: valeur} pour un statement INSERT."""
    m = _HEADER_INSERT.match(statement)
    if not m:
        raise SourceError(f"{filename} : INSERT illisible pour la table `{table}`.")
    parser = _ValueParser(statement)
    i = parser.skip_ws(m.end())
    row_index = 0
    n_cols = len(columns)
    text = statement
    while i < parser.n:
        if text[i] != "(":
            raise SourceError(
                f"{filename} : tuple inattendu dans l'INSERT de `{table}` (position {i})."
            )
        i += 1
        values = []
        while True:
            i = parser.skip_ws(i)
            if i >= parser.n:
                raise SourceError(f"{filename} : INSERT tronqué (`{table}`).")
            ch = text[i]
            if ch == ")":
                i += 1
                break
            if ch in ("'", '"'):
                try:
                    value, i = parser.parse_string(i)
                except ValueError as exc:
                    raise SourceError(f"{filename} : {exc} (`{table}`).") from None
                values.append(value)
            else:
                j = i
                while j < parser.n and text[j] not in ",)":
                    j += 1
                token = text[i:j].strip()
                i = j
                if token.upper() == "NULL" or token == "":
                    values.append(None)
                elif _INT_RE.match(token):
                    values.append(int(token))
                else:
                    values.append(token)
            i = parser.skip_ws(i)
            if i < parser.n and text[i] == ",":
                i += 1
                continue
            if i < parser.n and text[i] == ")":
                i += 1
                break
            raise SourceError(f"{filename} : séparateur inattendu dans l'INSERT de `{table}`.")
        if len(values) != n_cols:
            raise SourceError(
                f"{filename} : `{table}` ligne {row_index + 1} — {len(values)} valeurs "
                f"pour {n_cols} colonnes attendues."
            )
        row_index += 1
        yield dict(zip(columns, values))
        i = parser.skip_ws(i)
        if i < parser.n and text[i] == ",":
            i += 1
            continue
        break
