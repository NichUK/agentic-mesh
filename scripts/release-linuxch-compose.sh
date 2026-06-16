#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

DEFAULT_RUNTIME_BUILD_CONTEXT=/home/nich/agentic-mesh
if [ -d /mesh/system ]; then
  DEFAULT_RUNTIME_BUILD_CONTEXT=/mesh/system
fi

: "${AGENTIC_MESH_WORKSPACE_HOST_PATH:=/home/nich/agentic-mesh}"
: "${AGENTIC_MESH_RUNTIME_BUILD_CONTEXT:=$DEFAULT_RUNTIME_BUILD_CONTEXT}"
: "${AGENTIC_MESH_SYSTEM_HOST_PATH:=/home/nich/agentic-mesh}"
: "${AGENTIC_MESH_PROJECT_HOST_PATH:=/home/nich/agentic-mesh/examples/projects/agentic-mesh-dev}"
: "${AGENTIC_MESH_CODEX_HOME_HOST_PATH:=$AGENTIC_MESH_PROJECT_HOST_PATH/state/worker_mounts/codex-agentic-mesh-dev-team-home-q}"
: "${AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH:=/home/nich/agentic-mesh/config/otel/collector.yaml}"
: "${AGENTIC_MESH_URL_ROOT:=http://linuxch:8100}"
: "${AGENTIC_MESH_V3_STATUS_PORT:=8100}"
: "${AGENTIC_MESH_NATS_STATE_HOST_PATH:=$AGENTIC_MESH_PROJECT_HOST_PATH/state/v3/nats}"
: "${AGENTIC_MESH_RELEASE_SERVICES:=v3-nats v3-runtime}"
: "${AGENTIC_MESH_STOP_LEGACY_SERVICES:=1}"

export AGENTIC_MESH_WORKSPACE_HOST_PATH
export AGENTIC_MESH_RUNTIME_BUILD_CONTEXT
export AGENTIC_MESH_SYSTEM_HOST_PATH
export AGENTIC_MESH_PROJECT_HOST_PATH
export AGENTIC_MESH_CODEX_HOME_HOST_PATH
export AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH
export AGENTIC_MESH_URL_ROOT
export AGENTIC_MESH_V3_STATUS_PORT
export AGENTIC_MESH_FORCE_V3_STATUS_PORT="$AGENTIC_MESH_V3_STATUS_PORT"
export AGENTIC_MESH_NATS_STATE_HOST_PATH
export AGENTIC_MESH_RELEASE_SERVICES
export AGENTIC_MESH_STOP_LEGACY_SERVICES

cd "$REPO_ROOT"
mkdir -p "$AGENTIC_MESH_NATS_STATE_HOST_PATH"
if [ "$AGENTIC_MESH_STOP_LEGACY_SERVICES" = "1" ]; then
  legacy_container_ids="$(
    {
      docker ps -q --filter "name=agentic-mesh-v2-"
      docker ps -q --filter "name=agentic-mesh-agentic-mesh-dev-"
    } | sort -u
  )"
  if [ -n "$legacy_container_ids" ]; then
    printf '%s\n' "$legacy_container_ids" | xargs docker stop
  fi
fi
sh scripts/deploy-linuxch-compose.sh --profile build-image build runtime-image
sh scripts/deploy-linuxch-compose.sh --profile v3 up -d v3-nats
sh scripts/deploy-linuxch-compose.sh --profile v3 run --rm --no-deps v3-runtime \
  python -m agentic_mesh_v3.cli \
  --project-config /mesh/project/agentic-mesh/project-v3.yaml \
  preflight-live \
  --check-broker
sh scripts/deploy-linuxch-compose.sh --profile v3 up -d $AGENTIC_MESH_RELEASE_SERVICES
