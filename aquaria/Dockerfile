FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

# Index DB lives on a volume; music is bind-mounted read-only, Playlists read-write.
ENV AQUARIA_MUSIC_ROOT=/mnt/data/media/music/FLAC \
    AQUARIA_PLAYLIST_DIR=/mnt/data/media/music/Playlists \
    AQUARIA_DB=/data/aquaria.db \
    AQUARIA_PORT=8337

EXPOSE 8337
HEALTHCHECK --interval=60s --timeout=5s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"AQUARIA_PORT\"]}/healthz')"
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${AQUARIA_PORT}"]
