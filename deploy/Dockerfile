# Image for the containerised services: dashboard, aux worker (webhooks + R2 cleanup) and, with the
# "api" compose profile, the API itself (machines where host port 8080 is taken).
# Encode workers stay on the Windows host for NVENC; this image can also run `--role encode`
# with the x264 encoder (ENCODER=x264) on a CPU-only Linux box.
# Build context is the project root: docker compose -f deploy/docker-compose.yml build
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY upscale_core ./upscale_core
COPY server ./server
COPY tools ./tools
RUN useradd --system --uid 10001 app && mkdir -p logs data && chown -R app logs data
USER app

CMD ["python", "-m", "server.run_worker", "--role", "aux"]
