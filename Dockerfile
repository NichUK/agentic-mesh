FROM python:3.12-slim AS base-agent

WORKDIR /workspace

RUN apt-get update \
  && apt-get install -y --no-install-recommends bubblewrap ca-certificates curl gh git iproute2 jq lsof netcat-openbsd nodejs npm openssh-client procps ripgrep sqlite3 \
  && npm install -g @openai/codex@0.144.3 \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY docker/agentic-mesh-entrypoint.sh /usr/local/bin/agentic-mesh-entrypoint
RUN chmod +x /usr/local/bin/agentic-mesh-entrypoint

ENTRYPOINT ["agentic-mesh-entrypoint"]
CMD ["python", "-m", "agentic_mesh.cli", "status"]

FROM base-agent AS ops-agent

RUN apt-get update \
  && apt-get install -y --no-install-recommends docker-cli docker-compose docker.io iproute2 lsof netcat-openbsd openssh-client procps \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

FROM ops-agent AS dev-agent

RUN apt-get update \
  && apt-get install -y --no-install-recommends build-essential pkg-config python3-dev \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

FROM base-agent AS qa-agent

RUN apt-get update \
  && apt-get install -y --no-install-recommends chromium chromium-driver procps \
  && npm install -g playwright \
  && npx playwright install --with-deps chromium \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*
