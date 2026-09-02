"""Moteur ETL : ingestion cible, réconciliation multi-campus, fusion de comptes.

Déroulé (le tout DANS la transaction ouverte par la CLI) :
  1. capture des soldes cibles initiaux ;
  2. staging Paris (JSONB) ;
  3. passe « comptes » : Brest (streaming dump) + Paris (staging) -> index par
     clé de réconciliation (email, sinon pseudo, sinon prenom.nom) ;
  4. fusion des comptes présents sur les deux campus, insertion des users +
     wallets (un par campus, solde = solde source du campus) ;
  5. passe « comptabilité » : transactions + lignes de vente migrées avec
     remapping des identifiants utilisateurs (rattachées à leur campus) ;
  6. audit final (cf. audit.py).

Montants exclusivement en centimes entiers (int). Aucune écriture hors
transaction : la CLI décide du COMMIT (run) ou du ROLLBACK (dry-run / erreur).
"""

from collections import defaultdict

import sqlalchemy as sa

from . import audit, logfilter, settings
from .errors import AccountingError
from .parsing import files as files_reader
from .parsing.sqlstream import iter_business_rows
from .sources import brest as brest_contract
from .sources import map_line_row, map_transaction_row, map_user_row
from .sources import paris as paris_contract
from . import staging
from .util import normalize_key, utcnow as now_utc

_USERS_SQL = (
    "INSERT INTO users (first_name, last_name, promotion, username, password_hash, "
    "team_status, team_campus, team_title, photo, blacklist, blacklist_alcohol, created_at) "
    "VALUES (:first_name, :last_name, :promotion, :username, :password_hash, "
    ":team_status, :team_campus, :team_title, :photo, :blacklist, :blacklist_alcohol, "
    ":created_at) RETURNING id"
)
_WALLET_SQL = (
    "INSERT INTO wallets (user_id, campus, balance, glasses_outstanding) "
    "VALUES (:user_id, :campus, :balance, :glasses_outstanding)"
)
_TXN_SQL = (
    "INSERT INTO transactions (created_at, type, campus, total, operator_label, "
    "payment_method, deposit_glasses, deposit_user_id, from_user_id, to_user_id, note, "
    "cancelled, cancelled_at) "
    "VALUES (:created_at, :type, :campus, :total, :operator_label, :payment_method, "
    ":deposit_glasses, :deposit_user_id, :from_user_id, :to_user_id, :note, "
    ":cancelled, :cancelled_at) RETURNING id"
)
_LINE_SQL = (
    "INSERT INTO transaction_lines (transaction_id, article_id, article_name, "
    "article_type, quantity, unit_price, line_total) "
    "VALUES (:transaction_id, :article_id, :article_name, :article_type, :quantity, "
    ":unit_price, :line_total)"
)

# Sens du flux d'argent par type de transaction : qui est débité/crédité.
_USER_SIDE = {
    "achat": "from", "retrait": "from", "transfert": "from",
    "rechargement": "to", "direct": "to", "consigne": "deposit",
}


class SourceReader:
    """Lecture streaming + mapping des sources (aucune écriture).

    conn=None (mode --audit-only) : les records Paris sont lus directement
    depuis les fichiers, sans staging.
    """

    def __init__(self, source_files, money_unit, chunk_rows, conn=None):
        self.files = source_files
        self.money_unit = money_unit
        self.chunk_rows = chunk_rows
        self.conn = conn
        self.scan_stats = {}  # fichier -> SqlDumpScanner (le même fichier peut être
        # scanné plusieurs passes : on garde la dernière exécution par fichier)

    def _record_scan(self, filename, scanner):
        self.scan_stats[filename] = scanner

    # ---------------------------------------------------------- brest (SQL)

    def _iter_brest(self, entity_wanted):
        for src in self.files:
            if src.campus != "brest" or src.kind != "sql_dump":
                continue
            for table, _cols, rows in iter_business_rows(
                src.path,
                keep_map=brest_contract.TABLE_MAP,
                batch_size=self.chunk_rows,
                on_scan_done=lambda sc, s=src: self._record_scan(s.path.name, sc),
            ):
                entity = brest_contract.entity_for(table)
                if entity != entity_wanted:
                    continue
                for row in rows:
                    yield src.path.name, entity, row

    # ---------------------------------------------------------- paris

    def _iter_paris(self):
        if self.conn is not None:
            for source_file, source_table, record in staging.iter_records(
                self.conn, batch_size=self.chunk_rows
            ):
                yield source_file, source_table, record
            return
        for src in self.files:
            if src.campus != "paris":
                continue
            if src.kind == "sql_dump":
                def predicate(table_name):
                    return bool(table_name) and not logfilter.is_log_table(table_name)
                for table, _cols, rows in iter_business_rows(
                    src.path,
                    keep_predicate=predicate,
                    batch_size=self.chunk_rows,
                    on_scan_done=lambda sc, s=src: self._record_scan(s.path.name, sc),
                ):
                    for row in rows:
                        yield src.path.name, table, row
            else:
                for source_table, row in files_reader.iter_rows(src.path, src.kind):
                    yield src.path.name, source_table, row

    # ---------------------------------------------------------- flux publics

    def iter_users(self):
        """Passe comptes : (campus, source_file, user_canonique | None, warning)."""
        for filename, _entity, row in self._iter_brest("users"):
            user, warning = map_user_row(row, "brest", self.money_unit)
            yield "brest", filename, user, warning
        for filename, source_table, record in self._iter_paris():
            entity, obj = paris_contract.map_record(source_table, record, "paris", self.money_unit)
            if entity == "users":
                user, warning = (obj, None) if obj else (None, "champs insuffisants")
                yield "paris", filename, user, warning

    def _iter_brest_multi(self):
        for src in self.files:
            if src.campus != "brest" or src.kind != "sql_dump":
                continue
            for table, _cols, rows in iter_business_rows(
                src.path,
                keep_map=brest_contract.TABLE_MAP,
                batch_size=self.chunk_rows,
                on_scan_done=lambda sc, s=src: self._record_scan(s.path.name, sc),
            ):
                entity = brest_contract.entity_for(table)
                if entity not in ("transactions", "lines"):
                    continue
                for row in rows:
                    yield src.path.name, entity, row

    def iter_accounting_items(self):
        for filename, entity, row in self._iter_brest_multi():
            if entity == "transactions":
                yield "brest", filename, map_transaction_row(row, "brest", self.money_unit)
            else:
                line_obj = map_line_row(row, self.money_unit)
                if line_obj is not None:
                    yield "brest", filename, line_obj
        for filename, source_table, record in self._iter_paris():
            entity, obj = paris_contract.map_record(source_table, record, "paris", self.money_unit)
            if entity in ("transactions", "lines") and obj is not None:
                yield "paris", filename, obj


class Migrator:
    """Orchestre l'ingestion cible dans une connexion/transaction donnée."""

    def __init__(self, conn, source_files, chunk_rows=settings.DEFAULT_CHUNK_ROWS,
                 money_unit=settings.DEFAULT_MONEY_UNIT):
        self.conn = conn
        self.reader = SourceReader(source_files, money_unit, chunk_rows, conn=conn)
        self.chunk_rows = chunk_rows
        self.money_unit = money_unit
        self.source_sums = {"brest": 0, "paris": 0}
        self.accounts = {"brest": {}, "paris": {}}       # key -> canonical user
        self.key_order = {"brest": [], "paris": []}
        self.id_map = {"brest": {}, "paris": {}}         # src_id -> key
        self.target_id_by_key = {}
        self.txn_id_map = {"brest": {}, "paris": {}}     # src txn id -> target id
        self.usernames_taken = set()
        self.counts = defaultdict(int)
        self.initial_totals = audit.capture_target(conn)

    # ------------------------------------------------------------ comptes

    def load_accounts(self):
        for campus, filename, user, warning in self.reader.iter_users():
            self.counts[f"rows_users_{campus}"] += 1
            if user is None:
                self.counts["users_skipped"] += 1
                if warning:
                    self.counts[f"warn_{warning[:40]}"] += 1
                continue
            key = user["key"]
            if user["src_id"] is not None:
                # enregistré même si le compte est un doublon : les transactions
                # référencent l'id source et doivent rester traçables
                self.id_map[campus].setdefault(str(user["src_id"]), key)
            if key in self.accounts[campus]:
                self.counts[f"duplicates_{campus}"] += 1
                continue
            self.accounts[campus][key] = (user, filename)
            self.key_order[campus].append(key)
            self.source_sums[campus] += user["balance_cents"]

    def _unique_username(self, base, campus):
        if not base:
            return None
        base = normalize_key(base).replace(" ", "-")[:78] or None
        if base is None:
            return None
        candidate, i = base, 1
        while candidate in self.usernames_taken:
            i += 1
            candidate = f"{base}-{campus[0]}{i}"
        self.usernames_taken.add(candidate)
        return candidate

    def insert_users_and_wallets(self):
        # pré-charger les usernames déjà en base (cible non vierge)
        rows = self.conn.execute(
            sa.text("SELECT username FROM users WHERE username IS NOT NULL")
        ).fetchall()
        self.usernames_taken = {r[0] for r in rows}
        if self.initial_totals.users:
            self.counts["users_preexistants"] = self.initial_totals.users

        merged_keys = set(self.accounts["brest"]) & set(self.accounts["paris"])
        self.counts["users_merged"] = len(merged_keys)

        all_keys = list(self.key_order["brest"]) + [
            k for k in self.key_order["paris"] if k not in merged_keys
        ]
        for key in all_keys:
            campuses = [c for c in ("brest", "paris") if key in self.accounts[c]]
            primary = self.accounts[campus := campuses[0]][key][0]
            secondary = self.accounts[campuses[1]][key][0] if len(campuses) == 2 else None

            first = primary["first_name"] or (secondary["first_name"] if secondary else "")
            last = primary["last_name"] or (secondary["last_name"] if secondary else "")
            team_status = primary["team_status"] or (secondary["team_status"] if secondary else None)
            team_title = primary["team_title"] or (secondary["team_title"] if secondary else None)
            team_campus = primary["team_campus"] or (secondary["team_campus"] if secondary else None)
            if team_status is None and team_title:
                team_status = "mandat"
            if team_status and not team_campus:
                team_campus = campus
            photo = primary["photo"] or (secondary["photo"] if secondary else None)
            created_at = primary["created_at"] or (secondary["created_at"] if secondary else None)
            promotion = primary["promotion"] or (secondary["promotion"] if secondary else None)
            username = self._unique_username(primary["username"]
                                             or (secondary["username"] if secondary else None)
                                             or key.replace(" ", "."), campus)
            password_hash = primary["password_hash"] or (secondary["password_hash"] if secondary else None)
            if password_hash:
                self.counts["passwords_importes"] += 1
            else:
                self.counts["passwords_a_reinitialiser"] += 1

            params = {
                "first_name": (first or "?")[:80],
                "last_name": (last or "")[:80],
                "promotion": promotion,
                "username": username,
                "password_hash": password_hash,
                "team_status": team_status,
                "team_campus": team_campus,
                "team_title": team_title,
                "photo": photo,
                "blacklist": bool(primary["blacklist"] or (secondary and secondary["blacklist"])),
                "blacklist_alcohol": bool(primary["blacklist_alcohol"]
                                          or (secondary and secondary["blacklist_alcohol"])),
                "created_at": created_at or now_utc(),
            }
            user_id = self.conn.execute(sa.text(_USERS_SQL), params).scalar_one()
            self.target_id_by_key[key] = user_id

            for c in campuses:
                source_user, _ = self.accounts[c][key]
                self.conn.execute(sa.text(_WALLET_SQL), {
                    "user_id": user_id,
                    "campus": c,
                    "balance": source_user["balance_cents"],
                    "glasses_outstanding": source_user["glasses_outstanding"],
                })
                self.counts["wallets_created"] += 1
            self.counts["users_created"] += 1
        self.counts["wallets_preexistants"] = self.initial_totals.wallets

    # ------------------------------------------------------- comptabilité

    def _resolve_user_id(self, campus, ref):
        """Resout une référence utilisateur source (id ou email/pseudo) -> id cible."""
        if ref in (None, ""):
            return None
        ref_str = str(ref).strip()
        key = self.id_map[campus].get(ref_str)
        if key is None:
            key = self.id_map[campus].get(ref_str.lower())
        if key is not None and key in self.target_id_by_key:
            return self.target_id_by_key[key]
        norm = normalize_key(ref_str)
        if norm and norm in self.accounts[campus]:
            k = norm
            if k in self.target_id_by_key:
                return self.target_id_by_key[k]
        self.counts[f"txn_user_inconnu_{campus}"] += 1
        return None

    def insert_transactions(self):
        for campus, filename, obj in self.reader.iter_accounting_items():
            if "src_transaction_id" in obj:  # ligne de détail
                self._insert_line(campus, obj)
            else:
                self._insert_transaction(campus, filename, obj)

    def _insert_transaction(self, campus, filename, txn):
        params = {
            "created_at": txn["created_at"] or now_utc(),
            "type": txn["type"] or "direct",
            "campus": campus,
            "total": txn["total_cents"],
            "operator_label": txn["operator_label"][:120],
            "payment_method": txn["payment_method"],
            "deposit_glasses": txn["deposit_glasses"],
            "deposit_user_id": None,
            "from_user_id": None,
            "to_user_id": None,
            "note": txn["note"],
            "cancelled": bool(txn["cancelled"]),
            "cancelled_at": txn["cancelled_at"],
        }
        user_id = self._resolve_user_id(campus, txn["src_user_id"])
        side = _USER_SIDE.get(params["type"], "to")
        if side == "from":
            params["from_user_id"] = user_id
        elif side == "deposit":
            params["deposit_user_id"] = user_id
        else:
            params["to_user_id"] = user_id
        if txn["type"] is None:
            self.counts["txn_type_inconnu"] += 1
            base_note = (params["note"] or "")
            params["note"] = (f"[type source inconnu] {base_note}".strip())[:255] or None
        new_id = self.conn.execute(sa.text(_TXN_SQL), params).scalar_one()
        if txn["src_id"] is not None:
            self.txn_id_map[campus][str(txn["src_id"])] = new_id
        self.counts[f"txns_{campus}"] += 1
        if txn["cancelled"]:
            self.counts["txns_annulees"] += 1

    def _insert_line(self, campus, line_obj):
        txn_id = self.txn_id_map[campus].get(str(line_obj["src_transaction_id"]))
        if txn_id is None:
            self.counts["lignes_orphelines"] += 1
            return
        self.conn.execute(sa.text(_LINE_SQL), {
            "transaction_id": txn_id,
            "article_id": None,
            "article_name": line_obj["article_name"],
            "article_type": line_obj["article_type"],
            "quantity": line_obj["quantity"],
            "unit_price": line_obj["unit_price"],
            "line_total": line_obj["line_total"],
        })
        self.counts[f"lignes_{campus}"] += 1
        self.counts["lignes_importees"] += 1

    # ------------------------------------------------------------ global

    def migrate(self):
        """Exécute les passes et l'audit. Lève AccountingError si écart (rapport imprimé)."""
        staging.create(self.conn)
        stage_stats = staging.load(self.conn, self.reader.files, self.chunk_rows)
        self.counts["staging_rows"] = stage_stats["rows"]
        self.load_accounts()
        self.insert_users_and_wallets()
        self.insert_transactions()
        result = audit.AuditResult(
            source=dict(self.source_sums),
            initial=self.initial_totals,
            final=audit.capture_target(self.conn),
        )
        try:
            audit.check(result)
        except AccountingError:
            audit.print_audit(result, extra_counts=self.report_lines())
            raise
        return result

    def report_lines(self):
        """(label, valeur) pour le rapport final."""
        c = self.counts
        paris_projected = (c.get("rows_users_paris", 0) + c.get("txns_paris", 0)
                           + c.get("lignes_paris", 0))
        pairs = [
            ("Comptes créés", c.get("users_created")),
            ("Comptes fusionnés (2 campus)", c.get("users_merged")),
            ("Comptes ignorés (identité absente)", c.get("users_skipped")),
            ("Portefeuilles créés", c.get("wallets_created")),
            ("Mots de passe importés (hash compatible)", c.get("passwords_importes")),
            ("Mots de passe à réinitialiser", c.get("passwords_a_reinitialiser")),
            ("Transactions Brest", c.get("txns_brest")),
            ("Transactions Paris", c.get("txns_paris")),
            ("Transactions annulées (conservées)", c.get("txns_annulees")),
            ("Lignes de vente importées", c.get("lignes_importees")),
            ("dont Brest", c.get("lignes_brest")),
            ("dont Paris", c.get("lignes_paris")),
            ("Lignes orphelines ignorées", c.get("lignes_orphelines")),
            ("Transactions avec user introuvable", c.get("txn_user_inconnu_brest", 0)
             + c.get("txn_user_inconnu_paris", 0)),
            ("Types de transaction inconnus (-> direct)", c.get("txn_type_inconnu")),
            ("Lignes staging Paris", c.get("staging_rows")),
            ("Records Paris non projetés (hors entités)", c.get("staging_rows", 0)
             - paris_projected),
            ("Comptes préexistants en cible", c.get("users_preexistants")),
        ]
        return [(k, v) for k, v in pairs if v]
