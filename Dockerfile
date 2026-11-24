FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock* README.md ./
COPY src ./src

RUN uv pip install --no-cache .

RUN mkdir -p /app/data /app/backups/parquet

CMD ["solana-trading", "check-config"]
