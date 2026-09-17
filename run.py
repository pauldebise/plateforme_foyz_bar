import os

# run.py est le lanceur de développement (serveur local) : les secrets par
# défaut y sont tolérés avec avertissement. La production passe par wsgi.py.
os.environ.setdefault("FLASK_ENV", "development")

from app import create_app

app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=app.config.get("DEBUG", False))
