FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
    && apt-get install --no-install-recommends --yes gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 foyz \
    && mkdir -p /data/uploads \
    && rm -rf instance \
    && chown -R foyz:foyz /app /data/uploads

ENV UPLOAD_DIR=/data/uploads

USER foyz

EXPOSE 8000

# --max-requests : les workers sont recyclés (fuites mémoire des agrégations,
#   cf. phase Performance) ; --timeout : pas de requête bloquée indéfiniment ;
#   --error-logfile - : erreurs et traces dans les journaux du conteneur.
CMD ["gunicorn", "--workers", "4", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "--error-logfile", "-", "--timeout", "60", "--graceful-timeout", "30", "--max-requests", "1000", "--max-requests-jitter", "100", "wsgi:app"]
