"""Erreurs métier du module de migration."""


class MigrationError(Exception):
    """Erreur bloquante du pipeline de migration."""


class AccountingError(MigrationError):
    """Écart comptable détecté : la transaction doit être annulée."""


class SourceError(MigrationError):
    """Problème sur les fichiers sources (absents, non reconnus, illisibles)."""
