# QwenPawData Host worker image.
# Build context must be the QwenPaw-Data-Cloud repository root.
ARG BASE_IMAGE=python:3.12-slim-bookworm
FROM ${BASE_IMAGE}

ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com
ARG APT_MIRROR=
ARG SOURCE_REVISION=unknown

LABEL org.opencontainers.image.title="QwenPawData Cloud Host Worker" \
      org.opencontainers.image.revision="${SOURCE_REVISION}"

ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} \
    UV_LINK_MODE=copy \
    UV_DEFAULT_INDEX=${PIP_INDEX_URL} \
    UV_INDEX_URL=${PIP_INDEX_URL} \
    UV_INSECURE_HOST=${PIP_TRUSTED_HOST} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    QWENPAW_DATA_HOME=/data/qwenpaw-data \
    QWENPAW_DATA_LOG_PATH=/data/qwenpaw_data/logs/query.log \
    VIRTUAL_ENV=/opt/qwenpaw-data-cloud/.venv \
    PATH="/opt/qwenpaw-data-cloud/.venv/bin:/opt/uv-tool/bin:${PATH}"

RUN if [ -n "${APT_MIRROR}" ]; then \
        sed -i "s|deb.debian.org|${APT_MIRROR}|g; s|security.debian.org|${APT_MIRROR}|g" \
            /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/uv-tool \
    && /opt/uv-tool/bin/pip install --no-cache-dir \
        --index-url "${PIP_INDEX_URL}" \
        --trusted-host "${PIP_TRUSTED_HOST}" \
        uv

WORKDIR /opt/qwenpaw-data-cloud

COPY pyproject.toml uv.lock ./
COPY packages ./packages
COPY scripts ./scripts

RUN uv sync --all-packages --no-dev \
    && test -x /opt/qwenpaw-data-cloud/.venv/bin/python \
    && /opt/qwenpaw-data-cloud/.venv/bin/python -c "from qwenpaw_data.host.core.api.app import app; assert app is not None" \
    && mkdir -p /data/qwenpaw-data

EXPOSE 8089
VOLUME ["/data/qwenpaw-data"]

CMD ["bash", "-c", "exec /opt/qwenpaw-data-cloud/.venv/bin/python -m uvicorn qwenpaw_data.host.core.api.app:app --host 0.0.0.0 --port \"${QWENPAW_PORT:-8089}\""]
