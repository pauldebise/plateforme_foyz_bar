"""Audit comptable : captures des soldes cibles et vérification d'invariance.

Deux invariants indépendants (en centimes entiers) :

1. invariant de mapping — la somme BRUTE des colonnes de solde sources (toutes
   les lignes, y compris les comptes non mappés et les clés en collision) doit
   être égale à la somme réellement projetée vers les portefeuilles. Un écart
   prouve que de l'argent disparaît (cf. R4 : audit circulaire) ;
2. invariant de cible — somme(soldes sources projetés) == somme(soldes cibles
   après migration) − somme(soldes cibles avant).

Les deux sont vérifiés globalement ET par campus. Tout écart lève une
AccountingError : ROLLBACK et conservation des fichiers sources.

`AuditResult.raw_source` est la somme brute ; `AuditResult.source` reste la
somme projetée (rétrocompatible si `raw_source` est absent).
"""

import json
from dataclasses import dataclass, field

import sqlalchemy as sa

from .errors import AccountingError
from .report import kv, money, line, section
from .util import fmt_euros


@dataclass
class TargetTotals:
    total: int = 0
    brest: int = 0
    paris: int = 0
    wallets: int = 0
    users: int = 0


@dataclass
class AuditResult:
    source: dict = field(default_factory=dict)      # projeté {"brest": c, "paris": c}
    initial: TargetTotals = field(default_factory=TargetTotals)
    final: TargetTotals = field(default_factory=TargetTotals)
    raw_source: dict | None = None                  # brute (hors mapping)
    unmapped: list = field(default_factory=list)    # [(campus, fichier, cents)]
    collisions: list = field(default_factory=list)  # [(campus, clé, [occurrences])]

    @property
    def raw_total(self):
        return sum(self._raw_source.values())

    @property
    def _raw_source(self):
        return self.raw_source if self.raw_source is not None else self.source

    @property
    def source_total(self):
        return self.source.get("brest", 0) + self.source.get("paris", 0)

    def mapping_gap(self, campus=None):
        """Écart (doit valoir 0) : somme brute − somme projetée."""
        if campus:
            return self._raw_source.get(campus, 0) - self.source.get(campus, 0)
        return self.raw_total - self.source_total

    def delta(self, campus=None):
        """Écart de cible (doit valoir 0) : cible_migrée - source projetée."""
        if campus:
            migrated = self.final.__getattribute__(campus) - self.initial.__getattribute__(campus)
            return migrated - self.source.get(campus, 0)
        migrated = self.final.total - self.initial.total
        return migrated - self.source_total

    @property
    def ok(self):
        return (
            self.mapping_gap() == 0
            and self.mapping_gap("brest") == 0
            and self.mapping_gap("paris") == 0
            and self.delta() == 0
            and self.delta("brest") == 0
            and self.delta("paris") == 0
        )


def format_collisions(collisions, limit=50):
    """Rend lisibles les clés en collision : clé, occurrences et montants."""
    lines = []
    for campus, key, occurrences in collisions[:limit]:
        detail = ", ".join(
            f"id={o.get('src_id')} {o.get('name')!r} {fmt_euros(o.get('balance', 0))}"
            for o in occurrences
        )
        lines.append(f"[{campus}] {key!r} : {len(occurrences)} occurrences -> {detail}")
    if len(collisions) > limit:
        lines.append(f"… et {len(collisions) - limit} autre(s) clé(s) en collision.")
    return lines


def collision_payload(collisions):
    """Sérialisation JSON des collisions (pour un rapport exploitable)."""
    return json.dumps(
        [
            {"campus": campus, "key": key,
             "occurrences": [{"src_id": o.get("src_id"), "name": o.get("name"),
                              "balance_cents": o.get("balance", 0)} for o in occurrences]}
            for campus, key, occurrences in collisions
        ],
        ensure_ascii=False,
    )


def capture_target(conn):
    """Somme des soldes cibles (global + par campus) et compteurs."""
    row = conn.execute(
        sa.text(
            "SELECT COALESCE(SUM(balance), 0), COUNT(*) FROM wallets"
        )
    ).one()
    totals = TargetTotals(total=int(row[0]), wallets=int(row[1]))
    for campus in ("brest", "paris"):
        sub = conn.execute(
            sa.text(
                "SELECT COALESCE(SUM(balance), 0) FROM wallets WHERE campus = :c"
            ),
            {"c": campus},
        ).scalar_one()
        setattr(totals, campus, int(sub))
    totals.users = conn.execute(sa.text("SELECT COUNT(*) FROM users")).scalar_one()
    return totals


def check(result):
    """Lève AccountingError si l'un des deux invariants n'est pas strictement nul."""
    if result.ok:
        return
    details = []
    gaps = [
        ("mapping global", result.mapping_gap()),
        ("mapping brest", result.mapping_gap("brest")),
        ("mapping paris", result.mapping_gap("paris")),
        ("cible global", result.delta()),
        ("cible brest", result.delta("brest")),
        ("cible paris", result.delta("paris")),
    ]
    for label, value in gaps:
        if value != 0:
            details.append(f"{label} : écart de {value} centime(s)")
    message = (
        "ÉCART COMPTABLE DÉTECTÉ — transaction annulée, fichiers sources conservés. "
        + " | ".join(details)
    )
    if result.collisions:
        message += (
            f" | {len(result.collisions)} clé(s) de réconciliation en collision "
            "(fusion/homonymie à trancher avant bascule) :\n"
            + "\n".join(format_collisions(result.collisions))
        )
    if result.unmapped:
        total = sum(cents for _c, _f, cents in result.unmapped)
        message += (
            f" | {len(result.unmapped)} compte(s) source non mappé(s) "
            f"pour {fmt_euros(total)} : identité absente."
        )
    raise AccountingError(message)


def print_audit(result, extra_counts=None):
    """Rapport d'audit exhaustif en console."""
    section("RAPPORT D'AUDIT COMPTABLE")
    money("Solde BRUT source Brest", result._raw_source.get("brest", 0))
    money("Solde BRUT source Paris", result._raw_source.get("paris", 0))
    money("Solde BRUT source TOTAL", result.raw_total)
    money("Solde projeté Brest", result.source.get("brest", 0))
    money("Solde projeté Paris", result.source.get("paris", 0))
    money("Solde projeté TOTAL", result.source_total)
    kv("Écart de mapping global", f"{result.mapping_gap()} centime(s)")
    if result.mapping_gap() != 0:
        kv("  dont Brest", f"{result.mapping_gap('brest')} centime(s)")
        kv("  dont Paris", f"{result.mapping_gap('paris')} centime(s)")
        kv("Clés en collision", len(result.collisions))
        for detail in format_collisions(result.collisions, limit=20):
            line(f"      {detail}")
        if result.unmapped:
            kv("Comptes non mappés", len(result.unmapped))
    line()
    money("Solde cible avant migration", result.initial.total)
    money("Solde cible après migration", result.final.total)
    kv("Portefeuilles cibles", f"{result.final.wallets} (avant : {result.initial.wallets})")
    kv("Comptes cibles", f"{result.final.users} (avant : {result.initial.users})")
    line()
    money("Solde migré (cible delta)", result.final.total - result.initial.total)
    kv("Écart de cible global", f"{result.delta()} centime(s)")
    kv("Écart de cible Brest", f"{result.delta('brest')} centime(s)")
    kv("Écart de cible Paris", f"{result.delta('paris')} centime(s)")
    if extra_counts:
        line()
        for label, value in extra_counts:
            if value:
                kv(label, value)
    line()
    if result.ok:
        line("  >>> AUDIT VALIDÉ : mapping et cible strictement nuls (0 centime) <<<")
    else:
        line("  >>> AUDIT EN ÉCHEC : les soldes sources ne sont pas intégralement projetés <<<")
        line(f"      attendu migré : {fmt_euros(result.source_total)}")
    line()
