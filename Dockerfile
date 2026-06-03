FROM python:3.12-slim

WORKDIR /workspace

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

CMD ["python", "-m", "agentic_mesh.cli", "status"]
