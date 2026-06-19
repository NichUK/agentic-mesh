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
: "${AGENTIC_MESH_RELEASE_SERVICES:=v3-nats v3-runtime v3-supervisor otel-collector}"
: "${AGENTIC_MESH_ROLE_SERVICES:=agentic-mesh-dev-business-analyst-1 agentic-mesh-dev-delivery-manager-1 agentic-mesh-dev-enterprise-architect-1 agentic-mesh-dev-platform-engineer-1 agentic-mesh-dev-product-manager-1 agentic-mesh-dev-prompt-engineer-1 agentic-mesh-dev-project-manager-1 agentic-mesh-dev-research-analyst-1 agentic-mesh-dev-security-architect-1 agentic-mesh-dev-solution-architect-1 agentic-mesh-dev-engineering-1 agentic-mesh-dev-qa-engineer-1 agentic-mesh-dev-release-manager-1 agentic-mesh-dev-technical-writer-1 agentic-mesh-dev-ux-designer-1}"
: "${AGENTIC_MESH_SUPERVISOR_SERVICE:=v3-supervisor}"
: "${AGENTIC_MESH_STOP_ROLE_SERVICES_ON_RELEASE:=1}"
: "${AGENTIC_MESH_REMOVE_LEGACY_V2_CONTAINERS:=1}"

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
export AGENTIC_MESH_ROLE_SERVICES
export AGENTIC_MESH_SUPERVISOR_SERVICE
export AGENTIC_MESH_STOP_ROLE_SERVICES_ON_RELEASE
export AGENTIC_MESH_REMOVE_LEGACY_V2_CONTAINERS

cd "$REPO_ROOT"
mkdir -p "$AGENTIC_MESH_NATS_STATE_HOST_PATH"
sh scripts/deploy-linuxch-compose.sh --profile v3 stop "$AGENTIC_MESH_SUPERVISOR_SERVICE" >/dev/null 2>&1 || true
if [ "$AGENTIC_MESH_REMOVE_LEGACY_V2_CONTAINERS" = "1" ]; then
  legacy_v2_container_names="
agentic-mesh-v2-runtime-1
agentic-mesh-v2-teams-ingress-1
agentic-mesh-v2-supervisor-1
agentic-mesh-agentic-mesh-dev-business-analyst-1-1
agentic-mesh-agentic-mesh-dev-prompt-engineer-1-1
agentic-mesh-agentic-mesh-dev-ux-designer-1-1
agentic-mesh-agentic-mesh-dev-enterprise-architect-1-1
agentic-mesh-agentic-mesh-dev-solution-architect-1-1
agentic-mesh-agentic-mesh-dev-security-architect-1-1
agentic-mesh-agentic-mesh-dev-platform-engineer-1-1
agentic-mesh-agentic-mesh-dev-engineering-2-1
agentic-mesh-agentic-mesh-dev-technical-writer-1-1
agentic-mesh-agentic-mesh-dev-delivery-manager-1-1
agentic-mesh-agentic-mesh-dev-research-analyst-1-1
"
  for container_name in $legacy_v2_container_names; do
    docker rm -f "$container_name" >/dev/null 2>&1 || true
  done
fi
sh scripts/deploy-linuxch-compose.sh --profile build-image build runtime-image
sh scripts/deploy-linuxch-compose.sh --profile v3 up -d v3-nats
sh scripts/deploy-linuxch-compose.sh --profile v3 run --rm --no-deps v3-runtime \
  python -m agentic_mesh_v3.cli \
  --project-config /mesh/project/agentic-mesh/project-v3.yaml \
  preflight-live \
  --check-broker
sh scripts/deploy-linuxch-compose.sh --profile v3 run --rm --no-deps v3-runtime \
  python -m agentic_mesh_v3.cli \
  --project-config /mesh/project/agentic-mesh/project-v3.yaml \
  materialize-agent-configs \
  --image agentic-mesh:local \
  --source-repo /mesh/system \
  --deployed-runtime /mesh/system \
  --organisation-config-repo /mesh/system \
  --project-config-repo /mesh/project \
  --agent-config-root /mesh/project/state/v3/agent-configs \
  --runtime-state-dir /mesh/project/state/v3 \
  --document-library-root /mesh/project/documents \
  --role-templates-dir /mesh/system/config/roles \
  --local-dev-override
sh scripts/deploy-linuxch-compose.sh --profile v3 up -d --remove-orphans $AGENTIC_MESH_RELEASE_SERVICES
if [ "$AGENTIC_MESH_STOP_ROLE_SERVICES_ON_RELEASE" = "1" ] && [ "${AGENTIC_MESH_MIN_WARM_ROLE_INSTANCES:-0}" = "0" ]; then
  sh scripts/deploy-linuxch-compose.sh --profile v3 stop $AGENTIC_MESH_ROLE_SERVICES >/dev/null 2>&1 || true
  sh scripts/deploy-linuxch-compose.sh --profile v3 rm -f $AGENTIC_MESH_ROLE_SERVICES >/dev/null 2>&1 || true
fi
