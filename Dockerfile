FROM python:3.12-slim

WORKDIR /workspace

RUN apt-get update \
  && apt-get install -y --no-install-recommends git nodejs npm ripgrep \
  && npm install -g @openai/codex@0.135.0 \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

CMD ["python", "-m", "agentic_mesh.cli", "status"]
