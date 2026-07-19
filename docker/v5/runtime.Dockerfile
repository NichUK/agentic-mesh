ARG PYTHON_BASE_IMAGE=python:3.12.11-slim-bookworm@sha256:519591d6871b7bc437060736b9f7456b8731f1499a57e22e6c285135ae657bf7
ARG SOURCE_DATE_EPOCH=1751328000

FROM ${PYTHON_BASE_IMAGE} AS v5-runtime-build

ARG SOURCE_DATE_EPOCH
ENV SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONHASHSEED=0
WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src/agentic_mesh_v5 ./src/agentic_mesh_v5
RUN python -m pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .

FROM ${PYTHON_BASE_IMAGE} AS v5-runtime

ARG VCS_REF
RUN apt-get update \
    && apt-get install -y --no-install-recommends docker.io \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system --gid 10001 mesh \
    && adduser --system --uid 10001 --ingroup mesh --home /mesh mesh \
    && adduser mesh root
COPY --from=v5-runtime-build /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels \
    && python -m agentic_mesh_v5 --json boundary-check
WORKDIR /mesh
USER mesh
EXPOSE 8080
LABEL org.opencontainers.image.source="https://github.com/NichUK/agentic-mesh" \
      org.opencontainers.image.revision="${VCS_REF}" \
      io.agentic-mesh.runtime-family="v5"
HEALTHCHECK --interval=15s --timeout=5s --retries=8 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health/live', timeout=3).read()"]
ENTRYPOINT ["python", "-m", "agentic_mesh_v5"]
CMD ["api-serve", "--host", "0.0.0.0", "--port", "8080"]
