"""Module de migration des anciennes bases (Brest / Paris) vers la plateforme unifiée.

Pipeline ETL autonome exécutable en ligne de commande (voir migration/cli.py) :

    python -m migration --run          # migration complète + audit + suppression des sources
    python -m migration --dry-run      # cycle complet puis ROLLBACK, sources intactes
    python -m migration --audit-only   # comparaison des soldes sources / cibles sans injection

Invariants garanties :
- aucun log technique migré (filtrage par liste blanche de tables métier) ;
- montants exclusivement en centimes entiers (aucun flottant en base) ;
- somme BRUTE des soldes sources == somme projetée : tout écart (compte non
  mappé, clé de réconciliation en collision) bloque la bascule (audit T-5.1) ;
- somme projetée == somme des soldes cibles après − avant, écart strictement nul ;
- traitement cible dans une transaction unique (BEGIN ... COMMIT / ROLLBACK) ;
- lot marqué comme migré dans la même transaction : un rejeu est refusé (T-5.2) ;
- seuls les fichiers RÉELLEMENT lus sont supprimés, et uniquement après COMMIT.
"""
