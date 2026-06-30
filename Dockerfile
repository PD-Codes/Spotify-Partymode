# Spotify-Partymode container image
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application (static files now live inside the package).
COPY pyproject.toml ./
COPY spotify_partymode ./spotify_partymode

EXPOSE 8000

# Persist the SQLite database in a mounted volume by default.
ENV DATABASE_PATH=/data/partymode.db
VOLUME ["/data"]

CMD ["python", "-m", "spotify_partymode"]
