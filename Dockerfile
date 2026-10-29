# Unraid runs this as nobody:users, which is uid 99, gid 100.
# QSL_DB_PATH defaults to the mounted data dir. Render does not use
# this image; it still starts from render.yaml.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QSL_DB_PATH=/data/qsl_tracker.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && if ! getent group 100 >/dev/null; then groupadd --gid 100 users; fi \
    && useradd --uid 99 --gid 100 --no-create-home --shell /usr/sbin/nologin qsl \
    && mkdir -p /data \
    && chown 99:100 /data

COPY . .
RUN chown -R 99:100 /app

USER 99:100

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]

# Same one worker and 120s timeout as render.yaml, bound on all
# interfaces so the cloudflared container on the same network can
# reach it. Nothing publishes this port on the host.
CMD ["gunicorn", "app:app", "--workers", "1", "--timeout", "120", "--bind", "0.0.0.0:8000"]
