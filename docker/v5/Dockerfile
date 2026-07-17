ARG PYTHON_BASE_IMAGE=python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
ARG SOURCE_DATE_EPOCH=1751328000

FROM ${PYTHON_BASE_IMAGE} AS v5-worker-base

ARG CODEX_VERSION=0.144.3

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        jq \
        nodejs \
        npm \
        openssh-client \
        procps \
        ripgrep \
        tini \
    && npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY worker-healthcheck.py /usr/local/bin/agentic-mesh-worker-healthcheck
RUN chmod 0755 /usr/local/bin/agentic-mesh-worker-healthcheck \
    && mkdir -p /opt/agentic-mesh

LABEL org.opencontainers.image.source="https://github.com/NichUK/agentic-mesh" \
      org.opencontainers.image.version="0.1.0" \
      io.agentic-mesh.worker-family="v5"

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["codex", "--version"]

FROM v5-worker-base AS v5-general-worker

COPY manifests/general.json /opt/agentic-mesh/image-manifest.json
ENV AGENTIC_MESH_TOOL_PROFILE_ID=general
LABEL org.opencontainers.image.title="Agentic Mesh V5 general worker" \
      io.agentic-mesh.profile="general" \
      io.agentic-mesh.capabilities="filesystem.read,structured-output"
RUN agentic-mesh-worker-healthcheck
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["agentic-mesh-worker-healthcheck"]

FROM v5-worker-base AS v5-development-worker

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gh pkg-config python3-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
COPY manifests/development.json /opt/agentic-mesh/image-manifest.json
ENV AGENTIC_MESH_TOOL_PROFILE_ID=development
LABEL org.opencontainers.image.title="Agentic Mesh V5 development worker" \
      io.agentic-mesh.profile="development" \
      io.agentic-mesh.capabilities="build.execute,filesystem.read,filesystem.write,source-control.git,structured-output,test.execute"
RUN agentic-mesh-worker-healthcheck
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["agentic-mesh-worker-healthcheck"]

FROM v5-worker-base AS v5-qa-worker

RUN apt-get update \
    && apt-get install -y --no-install-recommends shellcheck \
    && python -m pip install --no-cache-dir pytest==8.4.2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
COPY manifests/qa.json /opt/agentic-mesh/image-manifest.json
ENV AGENTIC_MESH_TOOL_PROFILE_ID=qa
LABEL org.opencontainers.image.title="Agentic Mesh V5 QA worker" \
      io.agentic-mesh.profile="qa" \
      io.agentic-mesh.capabilities="filesystem.read,shell.lint,structured-output,test.execute"
RUN agentic-mesh-worker-healthcheck
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["agentic-mesh-worker-healthcheck"]

FROM v5-worker-base AS v5-operations-worker

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-compose \
        docker.io \
        gh \
        iproute2 \
        lsof \
        netcat-openbsd \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
COPY manifests/operations.json /opt/agentic-mesh/image-manifest.json
ENV AGENTIC_MESH_TOOL_PROFILE_ID=operations
LABEL org.opencontainers.image.title="Agentic Mesh V5 operations worker" \
      io.agentic-mesh.profile="operations" \
      io.agentic-mesh.capabilities="container.manage,filesystem.read,network.diagnose,source-control.git,structured-output"
RUN agentic-mesh-worker-healthcheck
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["agentic-mesh-worker-healthcheck"]

FROM v5-worker-base AS v5-ux-worker

ARG PLAYWRIGHT_VERSION=1.61.1
ARG AXE_PLAYWRIGHT_VERSION=4.12.1
ARG PIXELMATCH_VERSION=7.2.0
ARG PNGJS_VERSION=7.0.0
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN --mount=type=cache,target=/root/.npm \
    mkdir -p /opt/agentic-mesh/ux-tools \
    && npm install --prefix /opt/agentic-mesh/ux-tools --omit=dev --no-audit --no-fund \
        "@axe-core/playwright@${AXE_PLAYWRIGHT_VERSION}" \
        "pixelmatch@${PIXELMATCH_VERSION}" \
        "playwright@${PLAYWRIGHT_VERSION}" \
        "pngjs@${PNGJS_VERSION}" \
    && /opt/agentic-mesh/ux-tools/node_modules/.bin/playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends imagemagick poppler-utils \
    && ln -s /opt/agentic-mesh/ux-tools/node_modules/.bin/playwright /usr/local/bin/playwright \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /root/.cache
COPY ux-smoke-test.mjs /opt/agentic-mesh/ux-tools/ux-smoke-test.mjs
RUN chmod 0755 /opt/agentic-mesh/ux-tools/ux-smoke-test.mjs \
    && ln -s /opt/agentic-mesh/ux-tools/ux-smoke-test.mjs /usr/local/bin/agentic-mesh-ux-smoke
COPY manifests/ux.json /opt/agentic-mesh/image-manifest.json
ENV AGENTIC_MESH_TOOL_PROFILE_ID=ux
LABEL org.opencontainers.image.title="Agentic Mesh V5 UX worker" \
      io.agentic-mesh.profile="ux" \
      io.agentic-mesh.capabilities="accessibility.test,browser.test,filesystem.read,filesystem.write,image.inspect,pdf.inspect,structured-output,visual.compare"
RUN agentic-mesh-worker-healthcheck \
    && agentic-mesh-ux-smoke
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD ["agentic-mesh-worker-healthcheck"]
