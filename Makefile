# Foy'z & Bar — commandes projet

PYTHON ?= .venv/bin/python
SRC_DIR ?= bdd_a_migrer
ARCHIVE_DAYS ?= 30
PG_TEST_URL ?= postgresql+psycopg2://foyz:foyztest@127.0.0.1:55432/foyz_test

.PHONY: help init-db upgrade-db migrate-audit migrate-dry migrate-run migrate-keep \
        backup restore-list health purge-archives purge-logs \
        tests tests-postgres lint format audit

help:
	@echo "make init-db        cree/met a jour le schema + le mot de passe administrateur"
	@echo "make upgrade-db     applique les revisions Alembic en attente (production)"
	@echo "make migrate-audit  compare soldes sources/cibles de $(SRC_DIR)/ (sans injection)"
	@echo "make migrate-dry    cycle complet puis ROLLBACK, fichiers intacts"
	@echo "make migrate-run    migration + audit + SUPPRESSION des fichiers sources"
	@echo "make migrate-keep   comme migrate-run mais archive les sources (archives/<ts>/)"
	@echo "make backup         sauvegarde base + uploads (retention 7/4, hors-site optionnel)"
	@echo "make restore-list   liste les sauvegardes disponibles"
	@echo "make health         interroge /health (supervision)"
	@echo "make purge-archives detruit les archives de migration > $(ARCHIVE_DAYS) jours"
	@echo "make purge-logs     purge le registre des connexions (retention configuree)"
	@echo "make tests          execute toutes les suites (SQLite)"
	@echo "make tests-postgres memes suites sur PostgreSQL jetable ($(PG_TEST_URL))"
	@echo "make lint           ruff check (lint) + ruff format --check (format)"
	@echo "make format         applique ruff format"
	@echo "make audit          pip-audit sur requirements.txt"

init-db:
	$(PYTHON) -m flask --app wsgi.py init-db

upgrade-db:
	$(PYTHON) -m flask --app wsgi.py upgrade-db

migrate-audit:
	$(PYTHON) -m migration --audit-only --source-dir $(SRC_DIR)

migrate-dry:
	$(PYTHON) -m migration --dry-run --source-dir $(SRC_DIR)

migrate-run:
	$(PYTHON) -m migration --run --source-dir $(SRC_DIR)

migrate-keep:
	$(PYTHON) -m migration --run --keep-archives --source-dir $(SRC_DIR)

backup:
	$(PYTHON) -m ops.backup

restore-list:
	$(PYTHON) -m ops.restore --list

health:
	curl -fsS http://127.0.0.1:8000/health

purge-archives:
	$(PYTHON) -m migration --purge-archives $(ARCHIVE_DAYS)

purge-logs:
	$(PYTHON) -m flask --app wsgi.py purge-logs

tests:
	$(PYTHON) -m tests.run_all

tests-postgres:
	$(PYTHON) -m tests.run_all --postgres "$(PG_TEST_URL)"

lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format:
	$(PYTHON) -m ruff format .

audit:
	$(PYTHON) -m pip_audit -r requirements.txt
