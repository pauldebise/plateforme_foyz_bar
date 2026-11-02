import os

from dotenv import load_dotenv

# Le fichier `.env` doit être chargé avant l'import de `app` : `app.config`
# évalue ses attributs au chargement du module, si bien qu'un appel plus tardif
# (dans `create_app`) laisserait `SECRET_KEY`, `DATABASE_URL`, `ADMIN_PASSWORD`,
# `UPLOAD_DIR`, `HTTPS_ONLY`, etc. aux valeurs par défaut.
load_dotenv()

# run.py est le lanceur de développement (serveur local) : les secrets par
# défaut y sont tolérés avec avertissement. La production passe par wsgi.py.
os.environ.setdefault("FLASK_ENV", "development")

from app import create_app  # noqa: E402  (import après le chargement du .env)

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=app.config.get("DEBUG", False))
