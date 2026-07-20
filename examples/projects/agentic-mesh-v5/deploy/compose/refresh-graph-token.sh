#!/bin/sh
set -eu

: "${AGENTIC_MESH_DOCUMENT_CREDENTIAL_ROOT:?set the document credential root}"
: "${AGENTIC_MESH_GRAPH_AZURE_USER:?set the Azure CLI account owner}"
: "${AGENTIC_MESH_GRAPH_AZURE_CONFIG_DIR:?set the Azure CLI config directory}"
: "${AGENTIC_MESH_GRAPH_PROJECT_ID:?set the project id}"

project_id=$AGENTIC_MESH_GRAPH_PROJECT_ID
case "$project_id" in
  ''|*[!a-z0-9-]*|-*)
    echo "invalid Graph project id" >&2
    exit 2
    ;;
esac

control_uid=${AGENTIC_MESH_CONTROL_UID:-10001}
control_gid=${AGENTIC_MESH_CONTROL_GID:-10001}
credential_root=$(realpath -m "$AGENTIC_MESH_DOCUMENT_CREDENTIAL_ROOT")
case "$credential_root" in
  /*) ;;
  *)
    echo "document credential root must be absolute" >&2
    exit 2
    ;;
esac

path=$credential_root
for segment in graph oauth-cache projects "$project_id"; do
  path=$path/$segment
  install -d -m 0700 -o "$control_uid" -g "$control_gid" "$path"
done
target=$path/graph

token=$(
  runuser -u "$AGENTIC_MESH_GRAPH_AZURE_USER" -- \
    env AZURE_CONFIG_DIR="$AGENTIC_MESH_GRAPH_AZURE_CONFIG_DIR" \
    /usr/bin/az account get-access-token \
      --resource-type ms-graph \
      --query accessToken \
      --output tsv
)
case "$token" in
  ''|*[[:space:]]*)
    echo "Azure CLI returned an invalid Graph token" >&2
    exit 1
    ;;
esac
if [ "${#token}" -gt 65536 ]; then
  echo "Azure CLI returned an oversized Graph token" >&2
  exit 1
fi

temporary=$(mktemp "$path/.graph-token.XXXXXX")
trap 'rm -f "$temporary"' EXIT HUP INT TERM
printf '%s' "$token" >"$temporary"
chown "$control_uid:$control_gid" "$temporary"
chmod 0400 "$temporary"
mv -f "$temporary" "$target"
trap - EXIT HUP INT TERM
