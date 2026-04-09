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

# Copy backend application code.
COPY backend/app/ ./backend/app/

# Persistent data directory (mount a volume here in production).
RUN mkdir -p /data

ENV DATA_DIR=/data
ENV API_KEY=""

EXPOSE 8000

CMD uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000}
