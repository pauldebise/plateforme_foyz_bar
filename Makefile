# Foy'z & Bar — commandes projet

PYTHON ?= .venv/bin/python
SRC_DIR ?= bdd_a_migrer

.PHONY: help init-db seed migrate-audit migrate-dry migrate-run migrate-keep

help:
	@echo "make init-db        cree les tables + le mot de passe administrateur"
	@echo "make seed           jeu de donnees de demonstration"
	@echo "make migrate-audit  compare soldes sources/cibles de $(SRC_DIR)/ (sans injection)"
	@echo "make migrate-dry    cycle complet puis ROLLBACK, fichiers intacts"
	@echo "make migrate-run    migration + audit + SUPPRESSION des fichiers sources"
	@echo "make migrate-keep   comme migrate-run mais archive les sources (archives/<ts>/)"

init-db:
	$(PYTHON) -m flask --app wsgi.py init-db

seed:
	$(PYTHON) seed.py

migrate-audit:
	$(PYTHON) -m migration --audit-only --source-dir $(SRC_DIR)

migrate-dry:
	$(PYTHON) -m migration --dry-run --source-dir $(SRC_DIR)

migrate-run:
	$(PYTHON) -m migration --run --source-dir $(SRC_DIR)

migrate-keep:
	$(PYTHON) -m migration --run --keep-archives --source-dir $(SRC_DIR)
