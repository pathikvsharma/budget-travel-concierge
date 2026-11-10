# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080 \
    HOST=127.0.0.1 \
    TRAVEL_DATA_DIR=/app/data

WORKDIR /app

# Create dedicated least-privilege non-root runtime user and writable data dir.
RUN groupadd --gid 10001 appgroup \
    && useradd --uid 10001 --gid appgroup --shell /usr/sbin/nologin --create-home appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appgroup /app

COPY pyproject.toml README.md ./
COPY travel_concierge ./travel_concierge
COPY eval ./eval

RUN pip install --upgrade pip \
    && pip install .

USER appuser

EXPOSE 8080

# Cloud Run sets HOST=0.0.0.0 via service config only inside its managed ingress sandbox;
# local docker test runs default securely to HOST=127.0.0.1 unless explicitly overridden.
CMD ["sh", "-c", "exec adk api_server . --host \"${HOST}\" --port \"${PORT}\" --trace_to_cloud"]
