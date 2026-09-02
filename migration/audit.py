"""Audit comptable : captures des soldes cibles et vérification d'invariance.

Invariable absolu (en centimes entiers) :

    somme(soldes sources) == somme(soldes cibles après migration) - somme(soldes cibles avant)

soit un écart strictement nul, vérifié globalement ET par campus. Tout écart
lève une AccountingError : ROLLBACK et conservation des fichiers sources.
"""

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
    source: dict = field(default_factory=dict)      # {"brest": c, "paris": c}
    initial: TargetTotals = field(default_factory=TargetTotals)
    final: TargetTotals = field(default_factory=TargetTotals)

    @property
    def source_total(self):
        return self.source.get("brest", 0) + self.source.get("paris", 0)

    def delta(self, campus=None):
        """Écart (doit valoir 0) : cible_migrée - source."""
        if campus:
            migrated = self.final.__getattribute__(campus) - self.initial.__getattribute__(campus)
            return migrated - self.source.get(campus, 0)
        migrated = self.final.total - self.initial.total
        return migrated - self.source_total

    @property
    def ok(self):
        return self.delta() == 0 and self.delta("brest") == 0 and self.delta("paris") == 0


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
    """Lève AccountingError si l'écart n'est pas strictement nul."""
    if result.ok:
        return
    details = []
    for label, value in (
        ("global", result.delta()),
        ("brest", result.delta("brest")),
        ("paris", result.delta("paris")),
    ):
        if value != 0:
            details.append(f"{label} : écart de {value} centime(s)")
    raise AccountingError(
        "ÉCART COMPTABLE DÉTECTÉ — transaction annulée, fichiers sources conservés. "
        + " | ".join(details)
    )


def print_audit(result, extra_counts=None):
    """Rapport d'audit exhaustif en console."""
    section("RAPPORT D'AUDIT COMPTABLE")
    money("Solde source Brest", result.source.get("brest", 0))
    money("Solde source Paris", result.source.get("paris", 0))
    money("Solde source TOTAL", result.source_total)
    line()
    money("Solde cible avant migration", result.initial.total)
    money("Solde cible après migration", result.final.total)
    kv("Portefeuilles cibles", f"{result.final.wallets} (avant : {result.initial.wallets})")
    kv("Comptes cibles", f"{result.final.users} (avant : {result.initial.users})")
    line()
    money("Solde migré (cible delta)", result.final.total - result.initial.total)
    kv("Écart global", f"{result.delta()} centime(s)")
    kv("Écart Brest", f"{result.delta('brest')} centime(s)")
    kv("Écart Paris", f"{result.delta('paris')} centime(s)")
    if extra_counts:
        line()
        for label, value in extra_counts:
            if value:
                kv(label, value)
    line()
    if result.ok:
        line("  >>> AUDIT VALIDÉ : écart strictement nul (0 centime) <<<")
    else:
        line("  >>> AUDIT EN ÉCHEC : somme(soldes sources) != somme(soldes cibles) <<<")
        line(f"      attendu migré : {fmt_euros(result.source_total)}")
    line()
