# syntax=docker/dockerfile:1

FROM node:24-slim AS web
ENV PNPM_HOME=/pnpm PATH=/pnpm:$PATH
# Pinned major: the lockfile is v9 and an unpinned corepack would float.
RUN corepack enable && corepack prepare pnpm@10 --activate
WORKDIR /web
COPY web/package.json web/pnpm-lock.yaml web/pnpm-workspace.yaml ./
RUN --mount=type=cache,target=/pnpm/store \
    pnpm install --frozen-lockfile
COPY web/ ./
RUN pnpm build


FROM python:3.12-slim-trixie AS base
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
# UID/GID 1002 match the host `upload` user so bind-mounted data stays writable.
RUN groupadd -g 1002 app && useradd -u 1002 -g 1002 -m -s /usr/sbin/nologin app
ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1
WORKDIR /app


# Third-party deps only, so editing penish/ does not invalidate the install layer.
FROM base AS deps
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable


FROM base AS deps-worker
COPY pyproject.toml uv.lock ./
# torch is several hundred MB; keeping it above the code COPY keeps it cached.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable --extra worker


FROM deps AS api
COPY penish ./penish
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable
COPY --from=web /web/build /app/web
ENV WEB_BUILD_DIR=/app/web
USER app
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["uvicorn", "penish.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]


FROM deps-worker AS worker
COPY penish ./penish
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra worker
# Created before the volume mounts so the named volume inherits app ownership.
RUN mkdir /models && chown app:app /models
ENV HF_HOME=/models
USER app
CMD ["python", "-m", "penish.worker"]


# The VPS has only compose.yaml, so migrations ship in an image of their own.
FROM ghcr.io/amacneil/dbmate:2.38.1 AS migrate
COPY migrations /db/migrations
# No pg_dump in this image, and the schema dump is a dev convenience.
ENV DBMATE_MIGRATIONS_DIR=/db/migrations \
    DBMATE_NO_DUMP_SCHEMA=true
