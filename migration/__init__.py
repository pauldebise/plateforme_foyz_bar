"""Module de migration des anciennes bases (Brest / Paris) vers la plateforme unifiée.

Pipeline ETL autonome exécutable en ligne de commande (voir migration/cli.py) :

    python -m migration --run          # migration complète + audit + suppression des sources
    python -m migration --dry-run      # cycle complet puis ROLLBACK, sources intactes
    python -m migration --audit-only   # comparaison des soldes sources / cibles sans injection

Invariants garanties :
- aucun log technique migré (filtrage par liste blanche de tables métier) ;
- montants exclusivement en centimes entiers (aucun flottant en base) ;
- somme des soldes sources == somme des soldes cibles, écart strictement nul ;
- traitement cible dans une transaction unique (BEGIN ... COMMIT / ROLLBACK) ;
- fichiers de bdd_a_migrer/ supprimés uniquement après COMMIT validé.
"""
