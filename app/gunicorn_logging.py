"""Journal d'accès Gunicorn sans les jetons de passerelle.

L'URL `/passerelle/<jeton>` est un secret au porteur : le journal d'accès
(chemin, requête, référent) la masque. À activer avec
`--logger-class app.gunicorn_logging.MaskingLogger`.
"""

from gunicorn.glogging import Logger

from app.utils import redact_secrets


class MaskingLogger(Logger):
    def atoms(self, resp, req, environ, request_time):
        atoms = super().atoms(resp, req, environ, request_time)
        return {key: redact_secrets(value) for key, value in atoms.items()}
