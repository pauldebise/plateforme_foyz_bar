"""Journalisation exploitable : une ligne JSON par événement (production).

Objectifs (D6, D10) :
- les journaux partent sur stderr, collectés par Docker/systemd (rotation par
  le pilote de journalisation : voir docker-compose.yml) ;
- `LOG_FILE` active en plus un fichier tournant (10 Mo x 5) pour une
  installation sans superviseur ;
- les traces d'erreur sont jointes aux événements `unhandled_error`, ce qui
  permet d'alerter sur la page 500 (grep/alerte sur `"event": "unhandled_error"`).

`LOG_FORMAT=plain` rétablit un format lisible (développement). Aucune donnée
personnelle n'est ajoutée par ce module : les champs posés par les appelants
(`extra=`) sont repris tels quels.
"""

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_EXTRA_FIELDS = ("event", "path", "method", "endpoint", "status", "user_id")


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _EXTRA_FIELDS:
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)[:4000]
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(app):
    """Installe les handlers (idempotent : rappelé à chaque create_app)."""
    level = os.environ.get("LOG_LEVEL") or ("DEBUG" if app.debug else "INFO")
    json_output = os.environ.get("LOG_FORMAT", "").lower() != "plain" and not app.debug

    stream = logging.StreamHandler(sys.stderr)
    if json_output:
        stream.setFormatter(JsonFormatter())
    else:
        stream.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
        )

    root = logging.getLogger()
    root.setLevel(level)
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()
    root.addHandler(stream)

    log_file = os.environ.get("LOG_FILE")
    if log_file:
        rotating = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        rotating.setFormatter(JsonFormatter())
        root.addHandler(rotating)
    return root
