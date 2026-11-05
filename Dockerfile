# syntax=docker/dockerfile:1

FROM python:3.15-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.3 /uv /uvx /bin/

# Install dependencies first, in their own layer keyed only on the lockfile/manifest -
# this is the expensive step, and it must not be invalidated by app source changes
# (which happen on every PR) or every build reinstalls everything from scratch.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project

# Now layer in the project's own source and install it - cheap, since all
# dependencies are already resolved and cached above.
COPY app ./app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen

FROM base AS runtime
RUN useradd --create-home --uid 1000 appuser
COPY --from=builder /app/.venv /app/.venv
COPY app ./app

ENV PATH="/app/.venv/bin:$PATH"

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
