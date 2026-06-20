#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

: "${AGENTIC_MESH_WORKSPACE_HOST_PATH:=/home/nich/agentic-mesh}"
: "${AGENTIC_MESH_RUNTIME_BUILD_CONTEXT:=/home/nich/agentic-mesh}"
: "${AGENTIC_MESH_SYSTEM_HOST_PATH:=/home/nich/agentic-mesh}"
: "${AGENTIC_MESH_PROJECT_HOST_PATH:=/home/nich/agentic-mesh/examples/projects/agentic-mesh-dev}"
: "${AGENTIC_MESH_CODEX_HOME_HOST_PATH:=$AGENTIC_MESH_PROJECT_HOST_PATH/state/worker_mounts/codex-agentic-mesh-dev-team-home-q}"
: "${AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH:=/home/nich/agentic-mesh/config/otel/collector.yaml}"
: "${AGENTIC_MESH_URL_ROOT:=http://linuxch:8100}"
: "${AGENTIC_MESH_V4_STATUS_PORT:=8100}"
: "${AGENTIC_MESH_LIFECYCLE_LOCK_PATH:=$AGENTIC_MESH_PROJECT_HOST_PATH/state/compose-lifecycle.lock}"
: "${AGENTIC_MESH_RELEASE_SERVICES:=runtime dispatcher otel-collector}"
: "${AGENTIC_MESH_ROLE_SERVICES:=agentic-mesh-dev-project-manager-1 agentic-mesh-dev-delivery-manager-1 agentic-mesh-dev-product-manager-1 agentic-mesh-dev-business-analyst-1 agentic-mesh-dev-research-analyst-1 agentic-mesh-dev-enterprise-architect-1 agentic-mesh-dev-solution-architect-1 agentic-mesh-dev-security-architect-1 agentic-mesh-dev-ux-designer-1 agentic-mesh-dev-engineering-1 agentic-mesh-dev-qa-engineer-1 agentic-mesh-dev-platform-engineer-1 agentic-mesh-dev-release-manager-1 agentic-mesh-dev-technical-writer-1 agentic-mesh-dev-prompt-engineer-1}"
: "${AGENTIC_MESH_MIN_WARM_ROLE_INSTANCES:=0}"

export AGENTIC_MESH_WORKSPACE_HOST_PATH
export AGENTIC_MESH_RUNTIME_BUILD_CONTEXT
export AGENTIC_MESH_SYSTEM_HOST_PATH
export AGENTIC_MESH_PROJECT_HOST_PATH
export AGENTIC_MESH_CODEX_HOME_HOST_PATH
export AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH
export AGENTIC_MESH_URL_ROOT
export AGENTIC_MESH_V4_STATUS_PORT
export AGENTIC_MESH_LIFECYCLE_LOCK_PATH
export AGENTIC_MESH_RELEASE_SERVICES
export AGENTIC_MESH_ROLE_SERVICES

cd "$REPO_ROOT"
mkdir -p "$AGENTIC_MESH_PROJECT_HOST_PATH/state/v4"

sh scripts/deploy-linuxch-compose.sh --profile build-image build runtime-image

python -m agentic_mesh_v4.cli \
  --db "$AGENTIC_MESH_PROJECT_HOST_PATH/state/v4/agentic-mesh-v4.sqlite3" \
  --project-config "$AGENTIC_MESH_PROJECT_HOST_PATH/agentic-mesh/project-v4.yaml" \
  init-db

python -m agentic_mesh_v4.cli \
  --db "$AGENTIC_MESH_PROJECT_HOST_PATH/state/v4/agentic-mesh-v4.sqlite3" \
  --project-config "$AGENTIC_MESH_PROJECT_HOST_PATH/agentic-mesh/project-v4.yaml" \
  materialize-agent-configs \
  --agent-config-root "$AGENTIC_MESH_PROJECT_HOST_PATH/state/v4/agent-configs" \
  --role-templates-dir "$REPO_ROOT/config/roles"

python -m agentic_mesh_v4.cli \
  --db "$AGENTIC_MESH_PROJECT_HOST_PATH/state/v4/agentic-mesh-v4.sqlite3" \
  --project-config "$AGENTIC_MESH_PROJECT_HOST_PATH/agentic-mesh/project-v4.yaml" \
  render-compose \
  --output "$AGENTIC_MESH_PROJECT_HOST_PATH/deploy/compose/docker-compose.v4.yml"

sh scripts/deploy-linuxch-compose.sh --profile v4 up -d --remove-orphans $AGENTIC_MESH_RELEASE_SERVICES

if [ "$AGENTIC_MESH_MIN_WARM_ROLE_INSTANCES" = "0" ]; then
  sh scripts/deploy-linuxch-compose.sh --profile roles stop $AGENTIC_MESH_ROLE_SERVICES >/dev/null 2>&1 || true
  sh scripts/deploy-linuxch-compose.sh --profile roles rm -f $AGENTIC_MESH_ROLE_SERVICES >/dev/null 2>&1 || true
else
  sh scripts/deploy-linuxch-compose.sh --profile roles up -d $AGENTIC_MESH_ROLE_SERVICES
fi
