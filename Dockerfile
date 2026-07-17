# Spotify-Partymode container image
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Kopiere die Projektdatei und den Source-Code
COPY pyproject.toml ./
COPY spotify_partymode ./spotify_partymode

# Installiere das Paket und alle Abhängigkeiten direkt aus der pyproject.toml
RUN pip install --no-cache-dir .

EXPOSE 8000

# Persist the SQLite database in a mounted volume by default.
ENV DATABASE_PATH=/data/partymode.db
VOLUME ["/data"]

CMD ["python", "-m", "spotify_partymode"]
