"""Filtrage strict des tables de logs.

Principe : la liste blanche des tables métier réellement migrées est définie
dans migration/sources/brest.py (TABLE_MAP). Ce module fournit la
classification des tables *non migrées* pour le rapport d'audit :

- "log"     : table technique exclue volontairement (pattern explicite) ;
- "inconnue": table non reconnue, NON migrée par sécurité (conservateur).

Distinction clé : une table d'historique comptable (transactions, paiements)
n'est JAMAIS classée log — seuls les journaux techniques/connexions le sont.
"""

import re

# Motifs de tables techniques : journaux d'activité, connexions, sessions, debug.
_LOG_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^logs?$",  # log, logs
        r"^logs?_",  # log_actions, logs_actions...
        r"_logs?$",  # actions_log, connexion_logs...
        r"_logs?_",  # app_log_actions...
        r"^audit",  # audit, audit_events, audit_trail
        r"_audits?$",
        r"^connexions?$",  # connexion, connexions
        r"_connexions?$",
        r"^login",  # login_log, logins
        r"^sessions?$",  # session, sessions
        r"_sessions?$",
        r"^debug",  # debug_trace, debug_dump
        r"^traces?$",
        r"^journal",  # journal, journalisation
        r"_journals?$",
        r"^activit(y|e)",  # activity_log, activites
        r"_activites?$",
        r"^historique_(connexions?|actions?|evenements|events)",
        r"^phpbb_",
        r"^wp_",  # residuals d'outils tiers
        r"^django_(session|admin)_",
        r"^phinxlog$",
        r"^migrations$",  # tables d'outillage, pas de logs mais techniques
        r"^telemetry",
        r"^metrics$",
        r"^track",
    )
)

# Tables dont le nom évoque des logs mais qui peuvent être comptables : elles ne
# sont migrées que si elles figurent explicitement dans la liste blanche (TABLE_MAP),
# jamais sur simple reconnaissance de nom.
LOG_LOOKALIKE_HINTS = ("log", "audit", "historique", "journal")


def is_log_table(table_name):
    """True si le nom de table correspond à un journal technique/connexions/debug."""
    name = (table_name or "").strip().lower()
    return any(p.search(name) for p in _LOG_PATTERNS)


def classify(table_name):
    """Classement d'une table non migrée : "log", "inconnue" (ou None si migrée)."""
    if is_log_table(table_name):
        return "log"
    return "inconnue"
