"""Moteur ETL : ingestion cible, réconciliation multi-campus, fusion de comptes.

Déroulé (le tout DANS la transaction ouverte par la CLI) :
  1. capture des soldes cibles initiaux ;
  2. staging Paris (JSONB) ;
  3. passe « référence » : types d'articles Brest (id -> nom), préchargés pour
     résoudre le type des articles et des lignes de vente ;
  4. passe « comptes » : Brest (streaming dump) + Paris (staging) -> index par
     clé de réconciliation (email, sinon pseudo, sinon prenom.nom) ;
  5. fusion des comptes présents sur les deux campus, insertion des users +
     wallets (un par campus, solde = solde source du campus ; motif de
     blacklist importé) ;
  6. passe « catalogue » : articles (prix public/équipe), fûts pressions
     (kegs + keg_prices) et état courant des tireuses (taps + articles de
     tireuse régénérés comme app.services.catalog) ;
  7. passe « comptabilité » : achats + lignes de vente, rechargements
     (payments), retraits (withdrawals) et transferts (transfert, demi-lignes
     signées appariées) migrés avec remapping des identifiants utilisateurs,
     rattachement aux articles importés (article_id), contribution signée par
     étudiant et résolution du badge opérateur en nom de compte ;
  8. audit final (cf. audit.py).

Montants exclusivement en centimes entiers (int). Aucune écriture hors
transaction : la CLI décide du COMMIT (run) ou du ROLLBACK (dry-run / erreur).
"""

from collections import defaultdict
from datetime import datetime

import sqlalchemy as sa

from . import audit, logfilter, settings
from .errors import AccountingError
from .parsing import files as files_reader
from .parsing.sqlstream import iter_business_rows
from .sources import brest as brest_contract
from .sources import (TAP_NUMBERS, TAP_SIZES, map_article_row, map_keg_row,
                      map_line_row, map_operation_row, map_tap_row,
                      map_transaction_row, map_user_row)
from .sources import paris as paris_contract
from . import staging
from .util import normalize_key, utcnow as now_utc

_USERS_SQL = (
    "INSERT INTO users (name, promotion, password_hash, "
    "team_status, team_campus, blacklist, blacklist_alcohol, blacklist_reason, "
    "created_at) "
    "VALUES (:name, :promotion, :password_hash, "
    ":team_status, :team_campus, :blacklist, :blacklist_alcohol, :blacklist_reason, "
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
_CONTRIB_SQL = (
    "INSERT INTO contributions (transaction_id, user_id, campus, amount, balance_after) "
    "VALUES (:transaction_id, :user_id, :campus, :amount, 0)"
)
_ARTICLE_SQL = (
    "INSERT INTO articles (name, article_type, volume_cl, price_std_brest, "
    "price_std_paris, price_team_brest, price_team_paris, is_alcohol, is_tap, "
    "tap_number, keg_id, active, created_at) "
    "VALUES (:name, :article_type, :volume_cl, :price_std_brest, :price_std_paris, "
    ":price_team_brest, :price_team_paris, :is_alcohol, :is_tap, :tap_number, "
    ":keg_id, :active, :created_at) RETURNING id"
)
_KEG_SQL = (
    "INSERT INTO kegs (name, alcohol_degree, volume_l, remaining_l, active, created_at) "
    "VALUES (:name, :alcohol_degree, :volume_l, :remaining_l, :active, :created_at) "
    "RETURNING id"
)
_KEG_PRICE_SQL = (
    "INSERT INTO keg_prices (keg_id, campus, price_half_std, price_pint_std, "
    "price_pot_std, price_half_team, price_pint_team, price_pot_team) "
    "VALUES (:keg_id, :campus, :price_half_std, :price_pint_std, :price_pot_std, "
    ":price_half_team, :price_pint_team, :price_pot_team)"
)
_TAP_SQL = (
    "INSERT INTO taps (number, campus, keg_id) VALUES (:number, :campus, :keg_id)"
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
        """Streaming des tables Brest d'une entité (ou d'un ensemble d'entités)."""
        wanted = {entity_wanted} if isinstance(entity_wanted, str) else set(entity_wanted)
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
                if entity not in wanted:
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

    def iter_reference(self):
        """Passe référence : lignes brutes de la table Brest `article_types`."""
        for filename, _entity, row in self._iter_brest("article_types"):
            yield "brest", filename, row

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

    def iter_catalog(self, type_names=None):
        """Passe catalogue : (entité, campus, objet canonique).

        articles (catalogue), kegs (fûts pressions) et taps (occupations de
        tireuses) Brest — un seul streaming du dump pour les trois — puis
        articles Paris (staging).
        """
        for _filename, entity, row in self._iter_brest({"articles", "kegs", "taps"}):
            if entity == "articles":
                obj = map_article_row(row, self.money_unit, type_names)
            elif entity == "kegs":
                obj = map_keg_row(row, self.money_unit)
            else:
                obj = map_tap_row(row)
            if obj is not None:
                yield entity, "brest", obj
        for filename, source_table, record in self._iter_paris():
            entity, obj = paris_contract.map_record(source_table, record, "paris", self.money_unit)
            if entity == "articles" and obj is not None:
                yield "articles", "paris", obj

    def iter_transactions(self):
        """Passe comptabilité : (campus, fichier, entité, transaction).

        Brest : `transactions` (achats), `payments` (rechargements avec moyen
        de paiement), `withdrawals` (retraits) et `transfert` (demi-lignes
        signées à apparier) ; Paris : transactions.
        """
        for filename, entity, row in self._iter_brest(
            {"transactions", "rechargements", "retraits", "transferts"}
        ):
            if entity == "transactions":
                obj = map_transaction_row(row, "brest", self.money_unit)
                # la table Brest réelle n'a pas de colonne type : des achats.
                # Un type présent mais inconnu (type_warning) reste None pour
                # conserver la note « type source inconnu ».
                if obj["type"] is None and obj.get("type_warning") is None:
                    obj["type"] = "achat"
            else:
                obj = map_operation_row(row, brest_contract.ENTITY_FIELDS[entity],
                                        entity, "brest", self.money_unit)
            yield "brest", filename, entity, obj
        for filename, source_table, record in self._iter_paris():
            entity, obj = paris_contract.map_record(source_table, record, "paris", self.money_unit)
            if entity == "transactions" and obj is not None:
                yield "paris", filename, "transactions", obj

    def iter_lines(self, type_names=None):
        """Passe lignes de détail : après les transactions (remapping des ids).

        `type_names` : {id: nom} de la table Brest `article_types`, pour
        résoudre le type d'article des lignes (Brest `baskets.article_type`).
        """
        for filename, _entity, row in self._iter_brest("lines"):
            line_obj = map_line_row(row, self.money_unit, type_names)
            if line_obj is not None:
                yield "brest", filename, line_obj
        for filename, source_table, record in self._iter_paris():
            entity, obj = paris_contract.map_record(source_table, record, "paris", self.money_unit)
            if entity == "lines" and obj is not None:
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
        self.target_names = {}                           # id cible -> nom (opérateurs)
        self.txn_id_map = {"brest": {}, "paris": {}}     # src txn id -> target id
        self.type_names = {}                             # article_types id -> nom
        self.article_id_map = {}                         # src article id -> cible
        self.keg_id_map = {}                             # src keg id -> (cible, obj)
        self.tap_candidates = {}                         # tap n° -> (clé_tri, obj)
        self.names_taken = set()
        self.counts = defaultdict(int)
        self.initial_totals = audit.capture_target(conn)

    # ------------------------------------------------------------ référence

    def load_reference(self):
        """Précharge la table Brest `article_types` : {id: nom de type}.

        Le dump réel place `articles` AVANT `article_types` : une passe dédiée
        est nécessaire pour résoudre les types pendant la passe catalogue.
        """
        for _campus, _filename, row in self.reader.iter_reference():
            self.counts["refs_article_types"] += 1
            type_id = row.get("id")
            name = row.get("name")
            if type_id is None or not name:
                continue
            try:
                self.type_names[int(str(type_id).strip())] = str(name).strip()
            except ValueError:
                continue

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

    def _unique_name(self, base):
        """Rend le nom / surnom unique (insensible à la casse/accents)."""
        if not base:
            return None
        base = str(base).strip()[:80]
        norm = normalize_key(base)
        if norm is None:
            return None
        candidate, i = base, 1
        while normalize_key(candidate) in self.names_taken:
            i += 1
            suffix = f" {i}"
            candidate = base[: 80 - len(suffix)] + suffix
        self.names_taken.add(normalize_key(candidate))
        return candidate

    def insert_users_and_wallets(self):
        # pré-charger les noms déjà en base (cible non vierge)
        rows = self.conn.execute(
            sa.text("SELECT name FROM users")
        ).fetchall()
        self.names_taken = {normalize_key(r[0]) for r in rows if r[0]}
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

            name = primary["name"] or (secondary["name"] if secondary else None) or key
            team_status = primary["team_status"] or (secondary["team_status"] if secondary else None)
            team_campus = primary["team_campus"] or (secondary["team_campus"] if secondary else None)
            if team_status and not team_campus:
                team_campus = campus
            created_at = primary["created_at"] or (secondary["created_at"] if secondary else None)
            promotion = primary["promotion"] or (secondary["promotion"] if secondary else None)
            password_hash = primary["password_hash"] or (secondary["password_hash"] if secondary else None)
            if password_hash:
                self.counts["passwords_importes"] += 1
            else:
                self.counts["passwords_a_reinitialiser"] += 1

            params = {
                "name": self._unique_name(name) or "?",
                "promotion": promotion,
                "password_hash": password_hash,
                "team_status": team_status,
                "team_campus": team_campus,
                "blacklist": bool(primary["blacklist"] or (secondary and secondary["blacklist"])),
                "blacklist_alcohol": bool(primary["blacklist_alcohol"]
                                          or (secondary and secondary["blacklist_alcohol"])),
                "blacklist_reason": primary["blacklist_reason"]
                or (secondary["blacklist_reason"] if secondary else None),
                "created_at": created_at or now_utc(),
            }
            if params["blacklist_reason"]:
                self.counts["motifs_blacklist"] += 1
            user_id = self.conn.execute(sa.text(_USERS_SQL), params).scalar_one()
            self.target_id_by_key[key] = user_id
            self.target_names[user_id] = params["name"]

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

    # ------------------------------------------------------- catalogue

    def insert_catalog(self):
        """Catalogue Brest/Paris : articles, fûts pressions, tireuses courantes."""
        for entity, campus, obj in self.reader.iter_catalog(type_names=self.type_names):
            if entity == "articles":
                self._insert_article(campus, obj)
            elif entity == "kegs":
                self._insert_keg(obj)
            else:
                self._register_tap(obj)
        self._create_taps()

    def _insert_article(self, campus, obj):
        # les prix source ne valent que pour leur campus ; l'autre reste à 0
        if campus == "brest":
            std_b, team_b = obj["price_std_cents"], obj["price_team_cents"]
            std_p, team_p = 0, 0
        else:
            std_b, team_b = 0, 0
            std_p, team_p = obj["price_std_cents"], obj["price_team_cents"]
        params = {
            "name": obj["name"],
            "article_type": obj["article_type"],
            "volume_cl": obj["volume_cl"],
            "price_std_brest": std_b,
            "price_std_paris": std_p,
            "price_team_brest": team_b,
            "price_team_paris": team_p,
            "is_alcohol": obj["is_alcohol"],
            "is_tap": False,
            "tap_number": None,
            "keg_id": None,
            "active": obj["active"],
            "created_at": now_utc(),
        }
        article_id = self.conn.execute(sa.text(_ARTICLE_SQL), params).scalar_one()
        if obj["src_id"] is not None:
            self.article_id_map.setdefault(str(obj["src_id"]), article_id)
        self.counts[f"articles_{campus}"] += 1
        self.counts["articles_importes"] += 1

    def _insert_keg(self, obj):
        params = {
            "name": obj["name"],
            "alcohol_degree": obj["alcohol_degree"],
            "volume_l": obj["volume_l"],
            "remaining_l": obj["remaining_l"],
            "active": True,
            "created_at": now_utc(),
        }
        keg_id = self.conn.execute(sa.text(_KEG_SQL), params).scalar_one()
        self.conn.execute(sa.text(_KEG_PRICE_SQL), {
            "keg_id": keg_id,
            "campus": "brest",
            "price_half_std": obj["price_half_std"],
            "price_pint_std": obj["price_pint_std"],
            "price_pot_std": obj["price_pot_std"],
            "price_half_team": obj["price_half_team"],
            "price_pint_team": obj["price_pint_team"],
            "price_pot_team": obj["price_pot_team"],
        })
        self.counts["keg_prix_crees"] += 1
        if obj["src_id"] is not None:
            self.keg_id_map.setdefault(str(obj["src_id"]), (keg_id, obj))
        self.counts["kegs_importes"] += 1

    def _register_tap(self, obj):
        """Occurrence de tireuse source : ne garde que l'état courant le plus récent.

        `draft_beer_current` est un historique d'occupation (une ligne par
        intervalle) : les lignes closes (date_end) sont ignorées ; pour une
        même tireuse, l'ouverture la plus récente gagne (date_start, puis id).
        """
        if obj["date_end"] is not None:
            self.counts["taps_historiques"] += 1
            return
        number = TAP_NUMBERS.get(str(obj["draught"] or "").strip().lower())
        if number is None:
            self.counts["taps_draught_inconnu"] += 1
            return
        try:
            src_rank = int(str(obj["src_id"]))
        except (TypeError, ValueError):
            src_rank = 0
        key = (obj["date_start"] or datetime.min, src_rank)
        previous = self.tap_candidates.get(number)
        if previous is not None:
            self.counts["taps_doublons"] += 1
            if previous[0] >= key:
                return
        self.tap_candidates[number] = (key, obj)

    def _create_taps(self):
        """Crée les tireuses courantes + leurs articles (comme assign_keg)."""
        taken = {int(r[0]) for r in self.conn.execute(sa.text("SELECT number FROM taps"))}
        for number in sorted(self.tap_candidates):
            if number in taken:
                # cible non vierge : la tireuse existe déjà, on ne touche pas
                self.counts["taps_deja_presentes"] += 1
                continue
            _key, obj = self.tap_candidates[number]
            keg = self.keg_id_map.get(str(obj["src_keg_id"]))
            if keg is None:
                self.counts["taps_keg_inconnu"] += 1
                continue
            keg_id, keg_obj = keg
            self.conn.execute(sa.text(_TAP_SQL), {
                "number": number, "campus": "brest", "keg_id": keg_id,
            })
            self.counts["taps_creees"] += 1
            active = keg_obj["remaining_l"] > 0.01
            prices = {
                "demi": (keg_obj["price_half_std"], keg_obj["price_half_team"]),
                "pinte": (keg_obj["price_pint_std"], keg_obj["price_pint_team"]),
                "pot": (keg_obj["price_pot_std"], keg_obj["price_pot_team"]),
            }
            for key, (label, vol) in TAP_SIZES.items():
                std, team = prices[key]
                self.conn.execute(sa.text(_ARTICLE_SQL), {
                    "name": f'{label} de tireuse {number} "{keg_obj["name"]}"'[:160],
                    "article_type": "biere",
                    "volume_cl": vol,
                    "price_std_brest": std,
                    "price_std_paris": std,
                    "price_team_brest": team,
                    "price_team_paris": team,
                    "is_alcohol": True,
                    "is_tap": True,
                    "tap_number": number,
                    "keg_id": keg_id,
                    "active": active,
                    "created_at": now_utc(),
                })
                self.counts["articles_tireuse_generes"] += 1

    # ------------------------------------------------------- comptabilité

    def _lookup_user_id(self, campus, ref):
        """Id cible d'une référence utilisateur source (badge/id/pseudo), None sinon."""
        if ref in (None, ""):
            return None
        ref_str = str(ref).strip()
        key = self.id_map[campus].get(ref_str)
        if key is None:
            key = self.id_map[campus].get(ref_str.lower())
        if key is not None and key in self.target_id_by_key:
            return self.target_id_by_key[key]
        norm = normalize_key(ref_str)
        if norm and norm in self.accounts[campus] and norm in self.target_id_by_key:
            return self.target_id_by_key[norm]
        return None

    def _resolve_user_id(self, campus, ref):
        """Comme _lookup_user_id, mais compte les références non résolues."""
        user_id = self._lookup_user_id(campus, ref)
        if user_id is None and ref not in (None, ""):
            self.counts[f"txn_user_inconnu_{campus}"] += 1
        return user_id

    def _operator_label(self, campus, raw):
        """Badge `logged_user_id` source -> nom du compte opérateur.

        Les identifiants d'opérateur Brest sont des badges (users.card_id) :
        afficher le numéro brut en colonne « Opérateur » est illisible. Quand
        le badge correspond à un compte migré, son nom est utilisé ; sinon le
        numéro source est conservé tel quel.
        """
        ref = str(raw or "").strip()
        if not ref:
            return ""
        user_id = self._lookup_user_id(campus, ref)
        name = self.target_names.get(user_id) if user_id is not None else None
        if name:
            self.counts["operateurs_resolus"] += 1
            return name[:120]
        self.counts["operateurs_badge_inconnu"] += 1
        return ref[:120]

    def insert_transactions(self):
        """Transactions (quel que soit l'ordre des tables dans le dump) puis
        lignes de détail.

        Les demi-lignes de transfert Brest (`transfert`, une ligne signée par
        côté) sont mises en attente puis appariées par (date, opérateur,
        montant) : -X côté donneur, +X côté bénéficiaire.
        """
        pending_transfers = {}
        for campus, _filename, entity, obj in self.reader.iter_transactions():
            if entity == "transferts":
                self._buffer_transfer(pending_transfers.setdefault(campus, {}), obj)
                continue
            if entity in ("rechargements", "retraits") and obj["total_cents"] <= 0:
                self.counts["operations_montant_non_positif"] += 1
                continue
            self._insert_transaction(campus, obj, track_id=(entity == "transactions"))
        for campus, by_key in pending_transfers.items():
            self._flush_transfers(campus, by_key)
        for campus, filename, obj in self.reader.iter_lines(type_names=self.type_names):
            self._insert_line(campus, obj)

    def _buffer_transfer(self, by_key, obj):
        """Met en attente une demi-ligne de transfert (montant signé)."""
        if not obj["signed_cents"]:
            self.counts["transferts_montant_nul"] += 1
            return
        key = (obj["created_at"], obj["operator_label"], abs(obj["signed_cents"]))
        side = "neg" if obj["signed_cents"] < 0 else "pos"
        by_key.setdefault(key, {"neg": [], "pos": []})[side].append(obj)
        self.counts["demi_transferts_brest"] += 1

    def _flush_transfers(self, campus, by_key):
        """Apparie les demi-lignes et insère les transactions de transfert."""
        for key in sorted(by_key, key=lambda k: (k[0] or datetime.min, k[2])):
            negs, poss = by_key[key]["neg"], by_key[key]["pos"]
            while negs and poss:
                self._insert_transaction(campus, negs.pop(0), transfer_to=poss.pop(0)["src_user_id"],
                                         track_id=False)
            for obj in negs + poss:
                # demi-ligne sans pendant : conservée avec son seul côté connu
                self.counts["transferts_non_apparies"] += 1
                self._insert_transaction(campus, obj, track_id=False)

    def _insert_transaction(self, campus, txn, transfer_to=None, track_id=True):
        params = {
            "created_at": txn["created_at"] or now_utc(),
            "type": txn["type"] or "direct",
            "campus": campus,
            "total": txn["total_cents"],
            "operator_label": self._operator_label(campus, txn["operator_label"]),
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
        side = txn.get("side") or _USER_SIDE.get(params["type"], "to")
        if side == "from":
            params["from_user_id"] = user_id
        elif side == "deposit":
            params["deposit_user_id"] = user_id
        else:
            params["to_user_id"] = user_id
        if transfer_to is not None:
            params["to_user_id"] = self._resolve_user_id(campus, transfer_to)
        if txn["type"] is None:
            self.counts["txn_type_inconnu"] += 1
            base_note = (params["note"] or "")
            params["note"] = (f"[type source inconnu] {base_note}".strip())[:255] or None
        new_id = self.conn.execute(sa.text(_TXN_SQL), params).scalar_one()
        # seules les ventes sont référencées par des lignes de détail :
        # les autres tables sources réutilisent les mêmes id (1, 2, 3…)
        if track_id and txn["src_id"] is not None:
            self.txn_id_map[campus][str(txn["src_id"])] = new_id
        self._insert_contribution(campus, new_id, params)
        self.counts[f"txns_{campus}"] += 1
        self.counts[f"txns_type_{params['type']}"] += 1
        if txn["cancelled"]:
            self.counts["txns_annulees"] += 1

    def _insert_contribution(self, campus, txn_id, params):
        """Contribution signée rattachant l'étudiant à la transaction importée.

        Sans elle, les lignes migrées resteraient anonymes dans l'historique
        (libellé, filtre par étudiant, statistiques par profil). L'ancienne
        base ne conservant pas le solde résultant, balance_after reste à 0 :
        l'affichage masque le solde quand il est inconnu.
        """
        total = params["total"]
        entries = []
        if params["type"] == "transfert":
            if params["from_user_id"]:
                entries.append((params["from_user_id"], -total))
            if params["to_user_id"]:
                entries.append((params["to_user_id"], total))
        elif params["type"] == "direct":
            return
        else:
            user_id = (params["from_user_id"] or params["to_user_id"]
                       or params["deposit_user_id"])
            if user_id:
                entries.append((user_id, -total if params["from_user_id"] else total))
        for contrib_user_id, amount in entries:
            self.conn.execute(sa.text(_CONTRIB_SQL), {
                "transaction_id": txn_id, "user_id": contrib_user_id,
                "campus": campus, "amount": amount,
            })
            self.counts["contributions_importees"] += 1

    def _insert_line(self, campus, line_obj):
        txn_id = self.txn_id_map[campus].get(str(line_obj["src_transaction_id"]))
        if txn_id is None:
            self.counts["lignes_orphelines"] += 1
            return
        src_article = line_obj.get("src_article_id")
        article_id = self.article_id_map.get(str(src_article)) if src_article is not None else None
        if article_id is not None:
            self.counts["lignes_article_resolues"] += 1
        self.conn.execute(sa.text(_LINE_SQL), {
            "transaction_id": txn_id,
            "article_id": article_id,
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
        self.load_reference()
        self.load_accounts()
        self.insert_users_and_wallets()
        self.insert_catalog()
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
                           + c.get("lignes_paris", 0) + c.get("articles_paris", 0))
        pairs = [
            ("Comptes créés", c.get("users_created")),
            ("Comptes fusionnés (2 campus)", c.get("users_merged")),
            ("Comptes ignorés (identité absente)", c.get("users_skipped")),
            ("Portefeuilles créés", c.get("wallets_created")),
            ("Mots de passe importés (hash compatible)", c.get("passwords_importes")),
            ("Mots de passe à réinitialiser", c.get("passwords_a_reinitialiser")),
            ("Motifs de blacklist importés", c.get("motifs_blacklist")),
            ("Types d'articles (référence)", c.get("refs_article_types")),
            ("Articles importés", c.get("articles_importes")),
            ("dont Brest", c.get("articles_brest")),
            ("dont Paris", c.get("articles_paris")),
            ("Fûts pressions importés", c.get("kegs_importes")),
            ("Tarifs de fûts créés", c.get("keg_prix_crees")),
            ("Tireuses configurées", c.get("taps_creees")),
            ("Articles de tireuse générés", c.get("articles_tireuse_generes")),
            ("Occupations tireuse closes (historique)", c.get("taps_historiques")),
            ("Occupations tireuse doublons", c.get("taps_doublons")),
            ("Tireuses sans type reconnu", c.get("taps_draught_inconnu")),
            ("Tireuses avec fût introuvable", c.get("taps_keg_inconnu")),
            ("Tireuses déjà présentes en cible", c.get("taps_deja_presentes")),
            ("Transactions Brest", c.get("txns_brest")),
            ("Transactions Paris", c.get("txns_paris")),
            ("dont achats", c.get("txns_type_achat")),
            ("dont rechargements (payments)", c.get("txns_type_rechargement")),
            ("dont retraits (withdrawals)", c.get("txns_type_retrait")),
            ("dont transferts (demi-lignes appariées)", c.get("txns_type_transfert")),
            ("Transactions annulées (conservées)", c.get("txns_annulees")),
            ("Contributions étudiant créées", c.get("contributions_importees")),
            ("Opérateurs résolus (badge -> nom)", c.get("operateurs_resolus")),
            ("Opérateurs au badge inconnu (numéro conservé)", c.get("operateurs_badge_inconnu")),
            ("Demi-transferts reçus", c.get("demi_transferts_brest")),
            ("Transferts non appariés (côté seul conservé)", c.get("transferts_non_apparies")),
            ("Transferts à montant nul ignorés", c.get("transferts_montant_nul")),
            ("Rechargements/retraits à montant non positif ignorés",
             c.get("operations_montant_non_positif")),
            ("Lignes de vente importées", c.get("lignes_importees")),
            ("Lignes rattachées à un article", c.get("lignes_article_resolues")),
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
