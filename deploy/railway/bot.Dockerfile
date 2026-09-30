# HTTP-only bot image: no database, Redis, worker, or provider libraries/source.
FROM python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
COPY --from=ghcr.io/astral-sh/uv:0.11.15@sha256:e590846f4776907b254ac0f44b5b380347af5d90d668138ca7938d1b0c2f98d3 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app
WORKDIR /app
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-default-groups --group bot --no-install-project
COPY omni_logging.py ./
COPY bot ./bot
COPY deploy/railway/serve.py ./deploy/railway/serve.py
USER app
CMD ["python", "deploy/railway/serve.py", "bot"]
