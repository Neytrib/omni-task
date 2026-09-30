# Resolved official multi-platform image digests; update deliberately with dependency locks.
FROM python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS base
COPY --from=ghcr.io/astral-sh/uv:0.11.15@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
COPY pyproject.toml uv.lock ./
COPY omni_logging.py ./

FROM base AS backend
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN uv sync --locked --no-default-groups --group backend --no-install-project
COPY backend ./backend
COPY alembic.ini ./
COPY scripts ./scripts
ENV PYTHONPATH=/app/backend:/app
USER app
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log", "--log-level", "warning", "--ws", "websockets-sansio", "--ws-max-size", "1024"]

# Bot shares the base and HTTP stack but never receives ORM/broker clients or backend code.
FROM base AS bot
RUN uv sync --locked --no-default-groups --group bot --no-install-project
COPY bot ./bot
ENV PYTHONPATH=/app
USER app
CMD ["uvicorn", "bot.app.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]

FROM backend AS test
USER root
RUN uv sync --locked --no-default-groups --group backend --group bot --group dev --no-install-project
COPY tests ./tests
COPY bot ./bot
COPY docker-compose.yml compose.production.yml .dockerignore .env.example production.env.example ./
COPY deploy ./deploy
ENV PYTHONPATH=/app/backend:/app
USER app
CMD ["pytest", "-q"]
