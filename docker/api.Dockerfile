# Backend image — API, Celery worker, beat and Flower all run from this image.
# One image, several commands: it keeps the deployed code identical across
# processes, so a worker can never drift from the API it shares models with.

FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------- builder
FROM base AS builder

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml ./
COPY app ./app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --upgrade pip && pip install .

# ---------------------------------------------------------------- runtime
FROM base AS runtime

# Non-root: a container that does not need root should not have it.
RUN groupadd --system --gid 1001 twquant \
    && useradd --system --uid 1001 --gid twquant --create-home twquant

COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY --chown=twquant:twquant alembic.ini ./
COPY --chown=twquant:twquant alembic ./alembic
COPY --chown=twquant:twquant app ./app
COPY --chown=twquant:twquant scripts ./scripts

USER twquant
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
