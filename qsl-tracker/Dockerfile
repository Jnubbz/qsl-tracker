# QSL Tracker, for self-hosting on Unraid (or anywhere else with Docker).
#
# The SQLite database lives under /app/instance -- that directory is
# meant to be a bind-mounted volume (see docker-compose.yml) so it
# survives container recreates/updates. That's the whole point of
# moving off Render's free plan: its disk is ephemeral and wipes the
# database on every redeploy/spin-down, which defeats the purpose of
# the QSL Photo Map (see qsl-tracker-status.md in the project for the
# full writeup of why this move happened).
FROM python:3.11-slim

WORKDIR /app

# Install deps first so this layer stays cached across code-only rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Belt-and-suspenders -- docker-compose.yml's volume mount creates this
# too, but this way `docker build`/`docker run` alone (no compose) still
# works instead of gunicorn failing to open the SQLite file on first run.
RUN mkdir -p instance

EXPOSE 8000

# gunicorn, not `python app.py` -- same production server used on Render.
# Binding 0.0.0.0 (not 127.0.0.1) is what makes the container reachable
# from nginx/the host network at all.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "app:app"]
