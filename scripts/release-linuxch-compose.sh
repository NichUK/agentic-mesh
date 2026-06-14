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

export AGENTIC_MESH_WORKSPACE_HOST_PATH
export AGENTIC_MESH_RUNTIME_BUILD_CONTEXT
export AGENTIC_MESH_SYSTEM_HOST_PATH
export AGENTIC_MESH_PROJECT_HOST_PATH
export AGENTIC_MESH_CODEX_HOME_HOST_PATH
export AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH
export AGENTIC_MESH_URL_ROOT

cd "$REPO_ROOT"
sh scripts/deploy-linuxch-compose.sh --profile build-image build runtime-image
sh scripts/deploy-linuxch-compose.sh up -d ${AGENTIC_MESH_RELEASE_SERVICES:-v2-runtime v2-teams-ingress v2-supervisor}
