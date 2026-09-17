"""Exploitation : sauvegardes, restaurations et tâches de maintenance.

`python -m ops.backup`  : dump PostgreSQL + archive des téléversements,
                          rétention 7 quotidiennes / 4 hebdomadaires, envoi
                          hors-site optionnel (rsync).
`python -m ops.restore` : restauration sur un environnement vierge.

Aucune dépendance externe : PostgreSQL est sollicité via `pg_dump`/`pg_restore`
(installés localement) ou via `docker exec` dans le conteneur.
"""
