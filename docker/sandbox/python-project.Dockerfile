FROM kama-sandbox-python:3.12-v1

ARG KAMA_ENVIRONMENT_FINGERPRINT=unknown
LABEL kama.environment-fingerprint=$KAMA_ENVIRONMENT_FINGERPRINT

USER root
WORKDIR /opt/kama

RUN apt-get update \
    && apt-get install --no-install-recommends -y git ripgrep \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir . pytest pytest-asyncio ruff mypy build

WORKDIR /workspace
USER 10001:10001
