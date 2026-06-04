#!/bin/sh
set -eu

if command -v git >/dev/null 2>&1; then
  for safe_path in \
    "${AGENTIC_MESH_WORKSPACE_ROOT:-}" \
    "${AGENTIC_MESH_CONFIG_ROOT:-}" \
    "${AGENTIC_MESH_PROJECT_ROOT:-}" \
    /mesh/workspaces/agentic-mesh
  do
    if [ -n "$safe_path" ]; then
      git config --global --add safe.directory "$safe_path" >/dev/null 2>&1 || true
    fi
  done
fi

exec "$@"
