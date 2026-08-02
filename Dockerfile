FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /bin/
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OLLAMA_BASE_URL=http://host.docker.internal:11434 \
    DRY_RUN=true \

COPY pyproject.toml uv.lock ./
RUN uv sync --locked

COPY . .
RUN mkdir -p /app/logs /app/outputs

CMD ["sleep", "infinity"]
