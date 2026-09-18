FROM python:3.12-slim

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/workspace/src \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/cache

RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --no-create-home agent

WORKDIR /workspace
USER 10001:10001
CMD ["python", "--version"]
