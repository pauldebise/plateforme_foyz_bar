"""Campus connus et campus actifs.

Deux notions distinctes, volontairement séparées :

- ``KNOWN_CAMPUSES`` / ``CAMPUS_LABELS`` : tous les campus déjà vus par la
  plateforme. Ils servent uniquement à libeller des données historiques
  (portefeuilles, transactions, journaux, blacklists) sans jamais les exposer
  comme un campus opérationnel.
- ``ACTIVE_CAMPUSES`` / ``CAMPUSSES`` : les campus que l'application expose et
  sur lesquels elle écrit. La liste vient de ``FOYZ_ACTIVE_CAMPUSES``
  (codes séparés par des virgules, ordre conservé).

Un campus inactif est archivé, pas supprimé : ses lignes restent en base et
restent libellables, mais aucune interface, aucun service et aucun jeton ne
doit les considérer comme opérationnels.
"""

import os

ENV_VAR = "FOYZ_ACTIVE_CAMPUSES"
DEFAULT_RAW = "brest,paris"

KNOWN_CAMPUSES = {"brest": "Brest", "paris": "Paris"}
CAMPUS_LABELS = dict(KNOWN_CAMPUSES)


def parse_active_campuses(raw):
    """Convertit ``FOYZ_ACTIVE_CAMPUSES`` en tuple de codes connus.

    Refuse une valeur vide, un code inconnu ou une liste sans campus : une
    faute de configuration doit arrêter le démarrage, pas désactiver
    silencieusement un campus. Les doublons et la casse sont tolérés.
    """
    parts = [part.strip().lower() for part in str(raw or "").split(",")]
    keys = []
    unknown = []
    for key in parts:
        if not key:
            continue
        if key not in KNOWN_CAMPUSES:
            unknown.append(key)
        elif key not in keys:
            keys.append(key)
    if unknown:
        known = ", ".join(KNOWN_CAMPUSES)
        raise RuntimeError(
            f"{ENV_VAR} contient un campus inconnu ({', '.join(unknown)}). "
            f"Valeurs acceptées : {known}."
        )
    if not keys:
        raise RuntimeError(
            f"{ENV_VAR} doit contenir au moins un campus actif ({', '.join(KNOWN_CAMPUSES)})."
        )
    return tuple(keys)


ACTIVE_CAMPUSES = {
    key: KNOWN_CAMPUSES[key] for key in parse_active_campuses(os.environ.get(ENV_VAR, DEFAULT_RAW))
}
# Alias historique : les boucles d'interface et les validations utilisent les
# campus actifs. Les libellés de lignes historiques passent par CAMPUS_LABELS.
CAMPUSSES = ACTIVE_CAMPUSES
DEFAULT_CAMPUS = next(iter(ACTIVE_CAMPUSES))


def is_active(campus):
    """Vrai si le campus fait partie des campus actifs."""
    return campus in ACTIVE_CAMPUSES


def campus_label(campus):
    """Libellé lisible d'un campus, y compris historique ou inconnu."""
    if not campus:
        return "—"
    return CAMPUS_LABELS.get(campus, campus)
