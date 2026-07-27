FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

# index DB lives on a volume; music is bind-mounted read-only at runtime
ENV AQUARIA_MUSIC_ROOT=/mnt/data/media/music/FLAC \
    AQUARIA_DB=/data/aquaria.db \
    AQUARIA_PORT=8337

EXPOSE 8337
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8337"]
