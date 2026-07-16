#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

linuxch_host_path_or_default() {
  value=$1
  fallback=$2
  case "$value" in
    ""|/mesh|/mesh/*|/documents|/documents/*|/workspace|/workspace/*|/tmp|/tmp/*)
      printf '%s\n' "$fallback"
      ;;
    *)
      printf '%s\n' "$value"
      ;;
  esac
}

AGENTIC_MESH_RUNTIME_BUILD_CONTEXT=$(linuxch_host_path_or_default "${AGENTIC_MESH_RUNTIME_BUILD_CONTEXT:-}" /home/nich/agentic-mesh-system-clean)
AGENTIC_MESH_SYSTEM_HOST_PATH=$(linuxch_host_path_or_default "${AGENTIC_MESH_SYSTEM_HOST_PATH:-}" /home/nich/agentic-mesh-system-clean)
AGENTIC_MESH_PROJECT_HOST_PATH=$(linuxch_host_path_or_default "${AGENTIC_MESH_PROJECT_HOST_PATH:-}" /home/nich/agentic-mesh-projects/agentic-mesh-dev)
AGENTIC_MESH_WORKSPACE_HOST_PATH=$(linuxch_host_path_or_default "${AGENTIC_MESH_WORKSPACE_HOST_PATH:-}" "$AGENTIC_MESH_PROJECT_HOST_PATH/target-repos/agentic-mesh")
AGENTIC_MESH_DOCUMENTS_HOST_PATH=$(linuxch_host_path_or_default "${AGENTIC_MESH_DOCUMENTS_HOST_PATH:-}" "$AGENTIC_MESH_PROJECT_HOST_PATH/documents")
AGENTIC_MESH_CODEX_HOME_HOST_PATH=$(linuxch_host_path_or_default "${AGENTIC_MESH_CODEX_HOME_HOST_PATH:-}" "$AGENTIC_MESH_PROJECT_HOST_PATH/state/worker_mounts/codex-agentic-mesh-dev-team-home-q")
AGENTIC_MESH_GIT_SSH_HOST_PATH=$(linuxch_host_path_or_default "${AGENTIC_MESH_GIT_SSH_HOST_PATH:-${AGENTIC_MESH_PROJECT_MANAGER_SSH_HOST_PATH:-}}" /home/nich/.ssh)
AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH=$(linuxch_host_path_or_default "${AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH:-}" /home/nich/agentic-mesh-system-clean/config/otel/collector.yaml)
AGENTIC_MESH_RESTRICTED_SAFE_OUTPUT_CONFIG_HOST_PATH="$AGENTIC_MESH_PROJECT_HOST_PATH/deploy/codex/restricted-safe-output-config.toml"
AGENTIC_MESH_COMPOSE_STAGE_DIR=$(linuxch_host_path_or_default "${AGENTIC_MESH_COMPOSE_STAGE_DIR:-}" /home/nich/agentic-mesh-compose-run)
: "${AGENTIC_MESH_URL_ROOT:=http://linuxch:8100}"
: "${AGENTIC_MESH_V4_STATUS_PORT:=8100}"
: "${AGENTIC_MESH_DATABASE_NAME:=agentic_mesh_v4}"
: "${AGENTIC_MESH_DATABASE_USER:=agentic_mesh}"
: "${AGENTIC_MESH_DATABASE_HOST:=agentic-mesh-postgres}"
: "${AGENTIC_MESH_DATABASE_PORT:=5432}"
: "${AGENTIC_MESH_POSTGRES_HOST_PORT:=54329}"
: "${AGENTIC_MESH_DATABASE_PASSWORD_FILE:=$AGENTIC_MESH_PROJECT_HOST_PATH/state/secrets/postgres-password}"
: "${AGENTIC_MESH_HOST_DATABASE_HOST:=127.0.0.1}"
: "${AGENTIC_MESH_HOST_DATABASE_PORT:=$AGENTIC_MESH_POSTGRES_HOST_PORT}"
: "${AGENTIC_MESH_LIFECYCLE_LOCK_PATH:=$AGENTIC_MESH_PROJECT_HOST_PATH/state/compose-lifecycle.lock}"
: "${AGENTIC_MESH_RELEASE_SERVICES:=runtime dispatcher watchdog otel-collector}"
: "${AGENTIC_MESH_ROLE_SERVICES:=agentic-mesh-dev-project-manager-1 agentic-mesh-dev-delivery-manager-1 agentic-mesh-dev-product-manager-1 agentic-mesh-dev-business-analyst-1 agentic-mesh-dev-research-analyst-1 agentic-mesh-dev-enterprise-architect-1 agentic-mesh-dev-solution-architect-1 agentic-mesh-dev-security-architect-1 agentic-mesh-dev-ux-designer-1 agentic-mesh-dev-engineering-1 agentic-mesh-dev-qa-engineer-1 agentic-mesh-dev-platform-engineer-1 agentic-mesh-dev-release-manager-1 agentic-mesh-dev-technical-writer-1 agentic-mesh-dev-prompt-engineer-1}"
: "${AGENTIC_MESH_BASE_IMAGE_TAG:=agentic-mesh:base-agent}"
: "${AGENTIC_MESH_OPS_IMAGE_TAG:=agentic-mesh:ops-agent}"
: "${AGENTIC_MESH_DEV_IMAGE_TAG:=agentic-mesh:dev-agent}"
: "${AGENTIC_MESH_QA_IMAGE_TAG:=agentic-mesh:qa-agent}"
: "${AGENTIC_MESH_MIN_WARM_ROLE_INSTANCES:=0}"

GITHUB_SSH_PREFLIGHT_OUTPUT=""
if ! GITHUB_SSH_PREFLIGHT_OUTPUT=$(ssh -o BatchMode=yes -o ConnectTimeout=10 -o IdentitiesOnly=yes -i "$AGENTIC_MESH_GIT_SSH_HOST_PATH/id_rsa" -T git@github.com 2>&1); then
  case "$GITHUB_SSH_PREFLIGHT_OUTPUT" in
    *"successfully authenticated"*) ;;
    *)
      echo "Refusing to release: the approved shared Git SSH identity cannot authenticate to GitHub." >&2
      echo "Verify that $AGENTIC_MESH_GIT_SSH_HOST_PATH/id_rsa is registered and run: ssh -T git@github.com" >&2
      exit 1
      ;;
  esac
fi

if [ -d "$AGENTIC_MESH_RESTRICTED_SAFE_OUTPUT_CONFIG_HOST_PATH" ]; then
  echo "Refusing to release: restricted safe-output config target is a directory: $AGENTIC_MESH_RESTRICTED_SAFE_OUTPUT_CONFIG_HOST_PATH" >&2
  exit 1
fi

export AGENTIC_MESH_WORKSPACE_HOST_PATH
export AGENTIC_MESH_RUNTIME_BUILD_CONTEXT
export AGENTIC_MESH_SYSTEM_HOST_PATH
export AGENTIC_MESH_PROJECT_HOST_PATH
export AGENTIC_MESH_DOCUMENTS_HOST_PATH
export AGENTIC_MESH_CODEX_HOME_HOST_PATH
export AGENTIC_MESH_GIT_SSH_HOST_PATH
export AGENTIC_MESH_OTEL_COLLECTOR_CONFIG_HOST_PATH
export AGENTIC_MESH_URL_ROOT
export AGENTIC_MESH_V4_STATUS_PORT
export AGENTIC_MESH_DATABASE_NAME
export AGENTIC_MESH_DATABASE_USER
export AGENTIC_MESH_DATABASE_HOST
export AGENTIC_MESH_DATABASE_PORT
export AGENTIC_MESH_POSTGRES_HOST_PORT
export AGENTIC_MESH_DATABASE_PASSWORD_FILE
export AGENTIC_MESH_LIFECYCLE_LOCK_PATH
export AGENTIC_MESH_RELEASE_SERVICES
export AGENTIC_MESH_ROLE_SERVICES
export AGENTIC_MESH_COMPOSE_STAGE_DIR
export AGENTIC_MESH_PREFER_PARENT_PATHS=1

cd "$REPO_ROOT"
mkdir -p "$AGENTIC_MESH_PROJECT_HOST_PATH/state/v4"
mkdir -p "$AGENTIC_MESH_PROJECT_HOST_PATH/state/secrets" "$AGENTIC_MESH_PROJECT_HOST_PATH/state/postgres"
chmod 700 "$AGENTIC_MESH_PROJECT_HOST_PATH/state/secrets"
if [ ! -s "$AGENTIC_MESH_DATABASE_PASSWORD_FILE" ]; then
  umask 077
  openssl rand -base64 36 > "$AGENTIC_MESH_DATABASE_PASSWORD_FILE"
fi
chmod 600 "$AGENTIC_MESH_DATABASE_PASSWORD_FILE"
AGENTIC_MESH_HOST_DATABASE_PASSWORD=$(cat "$AGENTIC_MESH_DATABASE_PASSWORD_FILE")
if docker ps -a --format '{{.Names}}' | grep -qx 'agentic-mesh-postgres' \
  && ! docker port agentic-mesh-postgres 5432/tcp 2>/dev/null | grep -q "127.0.0.1:${AGENTIC_MESH_POSTGRES_HOST_PORT}"; then
  docker rm -f agentic-mesh-postgres >/dev/null
fi
if ! docker ps -a --format '{{.Names}}' | grep -qx 'agentic-mesh-postgres'; then
  docker run -d \
    --name agentic-mesh-postgres \
    --restart unless-stopped \
    --network agentic-mesh_default \
    -p "127.0.0.1:${AGENTIC_MESH_POSTGRES_HOST_PORT}:5432" \
    -e "POSTGRES_DB=$AGENTIC_MESH_DATABASE_NAME" \
    -e "POSTGRES_USER=$AGENTIC_MESH_DATABASE_USER" \
    -e "POSTGRES_PASSWORD=$AGENTIC_MESH_HOST_DATABASE_PASSWORD" \
    -v "$AGENTIC_MESH_PROJECT_HOST_PATH/state/postgres:/var/lib/postgresql/data" \
    postgres:16-alpine >/dev/null
else
  docker start agentic-mesh-postgres >/dev/null
fi
for i in $(seq 1 45); do
  if docker exec agentic-mesh-postgres pg_isready -U "$AGENTIC_MESH_DATABASE_USER" -d "$AGENTIC_MESH_DATABASE_NAME" >/dev/null 2>&1; then
    break
  fi
  if [ "$i" = "45" ]; then
    docker logs --tail 80 agentic-mesh-postgres >&2
    exit 1
  fi
  sleep 1
done
export AGENTIC_MESH_DATABASE_HOST="$AGENTIC_MESH_HOST_DATABASE_HOST"
export AGENTIC_MESH_DATABASE_PORT="$AGENTIC_MESH_HOST_DATABASE_PORT"
export AGENTIC_MESH_DATABASE_PASSWORD="$AGENTIC_MESH_HOST_DATABASE_PASSWORD"

PYTHONPATH="$REPO_ROOT/src" python -m agentic_mesh_v4.cli \
  --project-config "$AGENTIC_MESH_PROJECT_HOST_PATH/agentic-mesh/project-v4.yaml" \
  materialize-agent-configs \
  --agent-config-root "$AGENTIC_MESH_PROJECT_HOST_PATH/state/v4/agent-configs" \
  --role-templates-dir "$REPO_ROOT/config/roles"

PYTHONPATH="$REPO_ROOT/src" python -m agentic_mesh_v4.cli \
  --project-config "$AGENTIC_MESH_PROJECT_HOST_PATH/agentic-mesh/project-v4.yaml" \
  render-compose \
  --output "$AGENTIC_MESH_PROJECT_HOST_PATH/deploy/compose/docker-compose.v4.yml"

# The LinuxCH overlay is a reviewed deployment input from the system source.
# Refresh the dogfood project copy so a stale mutable project checkout cannot
# change which Agentic Mesh source tree the control plane imports.
cp "$REPO_ROOT/examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.linuxch.yml" \
  "$AGENTIC_MESH_PROJECT_HOST_PATH/deploy/compose/docker-compose.linuxch.yml"
mkdir -p "$(dirname "$AGENTIC_MESH_RESTRICTED_SAFE_OUTPUT_CONFIG_HOST_PATH")"
cp --remove-destination \
  "$REPO_ROOT/examples/projects/agentic-mesh-dev/deploy/codex/restricted-safe-output-config.toml" \
  "$AGENTIC_MESH_RESTRICTED_SAFE_OUTPUT_CONFIG_HOST_PATH"
export AGENTIC_MESH_DATABASE_HOST="agentic-mesh-postgres"
export AGENTIC_MESH_DATABASE_PORT="5432"
unset AGENTIC_MESH_DATABASE_PASSWORD
cp "$AGENTIC_MESH_PROJECT_HOST_PATH/deploy/compose/docker-compose.v4.yml" \
  "$AGENTIC_MESH_PROJECT_HOST_PATH/deploy/compose/docker-compose.yml"

if grep -Eq "/mesh/(workspaces/agentic-mesh|project)/src" \
  "$AGENTIC_MESH_PROJECT_HOST_PATH/deploy/compose/docker-compose.v4.yml" \
  "$AGENTIC_MESH_PROJECT_HOST_PATH/deploy/compose/docker-compose.linuxch.yml"; then
  echo "Refusing to release: V4 compose points control-plane PYTHONPATH at a mutable project or workspace source tree." >&2
  exit 1
fi
if ! grep -q "PYTHONPATH: /mesh/system/src" "$AGENTIC_MESH_PROJECT_HOST_PATH/deploy/compose/docker-compose.v4.yml"; then
  echo "Refusing to release: generated V4 compose is missing control-plane PYTHONPATH=/mesh/system/src." >&2
  exit 1
fi

sh scripts/deploy-linuxch-compose.sh --profile build-image build base-agent-image ops-agent-image dev-agent-image qa-agent-image

sh scripts/deploy-linuxch-compose.sh --profile v4 stop $AGENTIC_MESH_RELEASE_SERVICES >/dev/null 2>&1 || true
sh scripts/deploy-linuxch-compose.sh --profile roles stop $AGENTIC_MESH_ROLE_SERVICES >/dev/null 2>&1 || true
sh scripts/deploy-linuxch-compose.sh --profile roles rm -f $AGENTIC_MESH_ROLE_SERVICES >/dev/null 2>&1 || true

docker run --rm \
  --network agentic-mesh_default \
  -e "AGENTIC_MESH_DATABASE_HOST=$AGENTIC_MESH_DATABASE_HOST" \
  -e "AGENTIC_MESH_DATABASE_PORT=$AGENTIC_MESH_DATABASE_PORT" \
  -e "AGENTIC_MESH_DATABASE_NAME=$AGENTIC_MESH_DATABASE_NAME" \
  -e "AGENTIC_MESH_DATABASE_USER=$AGENTIC_MESH_DATABASE_USER" \
  -e "AGENTIC_MESH_DATABASE_PASSWORD_FILE=/mesh/project/state/secrets/postgres-password" \
  -e "PYTHONPATH=/mesh/system/src" \
  -v "$AGENTIC_MESH_SYSTEM_HOST_PATH:/mesh/system:ro" \
  -v "$AGENTIC_MESH_PROJECT_HOST_PATH:/mesh/project" \
  "$AGENTIC_MESH_OPS_IMAGE_TAG" \
  python -m agentic_mesh_v4.cli \
    --project-config /mesh/project/agentic-mesh/project-v4.yaml \
    init-db

sh scripts/deploy-linuxch-compose.sh --profile v4 up -d --force-recreate --remove-orphans $AGENTIC_MESH_RELEASE_SERVICES

if [ "$AGENTIC_MESH_MIN_WARM_ROLE_INSTANCES" != "0" ]; then
  sh scripts/deploy-linuxch-compose.sh --profile roles up -d --force-recreate $AGENTIC_MESH_ROLE_SERVICES
fi
