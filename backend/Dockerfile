FROM python:3.12-slim

WORKDIR /app

# Install system deps required by imageio/ffmpeg and Pillow.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy and install the ui-blueprint package first (leverages layer cache).
COPY pyproject.toml README.md ./
COPY ui_blueprint/ ./ui_blueprint/
COPY schema/ ./schema/
RUN pip install --no-cache-dir ".[video]"

# Install backend dependencies.
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy backend application code and migration files.
COPY backend/alembic.ini ./backend/alembic.ini
COPY backend/alembic/ ./backend/alembic/
COPY backend/entrypoint.sh ./backend/entrypoint.sh
COPY backend/worker-entrypoint.sh ./backend/worker-entrypoint.sh
COPY backend/app/ ./backend/app/

# Persistent data directory (mount a volume here in production).
RUN mkdir -p /data

ENV DATA_DIR=/data
ENV API_KEY=""

EXPOSE 8000

ENTRYPOINT ["bash", "backend/entrypoint.sh"]
