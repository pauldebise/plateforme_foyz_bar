FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 10001 foyz \
    && mkdir -p /data/uploads \
    && rm -rf instance \
    && chown -R foyz:foyz /app /data/uploads

ENV UPLOAD_DIR=/data/uploads

USER foyz

EXPOSE 8000

CMD ["gunicorn", "--workers", "4", "--bind", "0.0.0.0:8000", "--access-logfile", "-", "wsgi:app"]
