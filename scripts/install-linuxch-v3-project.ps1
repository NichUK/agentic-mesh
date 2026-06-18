param(
    [string]$LinuxHost = "nich@linuxch",
    [string]$RepoPath = "/home/nich/agentic-mesh",
    [string]$ProjectFile = "examples/projects/agentic-mesh-dev/agentic-mesh/project-v3.yaml",
    [string]$OrganizationFile = "config/organization.yaml",
    [string]$EnvPath = "examples/projects/agentic-mesh-dev/deploy/compose/.env",
    [string]$TeamsAppPackageRoot = "examples/projects/agentic-mesh-dev/deploy/teams-apps",
    [switch]$Apply,
    [switch]$AllowCreateTeam,
    [switch]$AllowCreateChannel,
    [switch]$AllowRegisterApps,
    [switch]$AllowInstallApps,
    [switch]$AllowUninstallStale,
    [switch]$AllowSecretRotation
)

$ErrorActionPreference = "Stop"

function Quote-Sh([string]$Value) {
    return "'" + ($Value -replace "'", "'\\''") + "'"
}

$argsList = @(
    "python", "-m", "agentic_mesh_v3.cli", "install-project",
    "--project-file", (Quote-Sh $ProjectFile),
    "--organization-file", (Quote-Sh $OrganizationFile),
    "--graph-token-file", "/tmp/agentic-mesh-v3-graph-token.json",
    "--teams-app-package-root", (Quote-Sh $TeamsAppPackageRoot)
)
if ($Apply) { $argsList += "--apply" }
if ($AllowCreateTeam) { $argsList += "--allow-create-team" }
if ($AllowCreateChannel) { $argsList += "--allow-create-channel" }
if ($AllowRegisterApps) { $argsList += "--allow-register-apps" }
if ($AllowInstallApps) { $argsList += "--allow-install-apps" }
if ($AllowUninstallStale) { $argsList += "--allow-uninstall-stale" }
if ($AllowSecretRotation) { $argsList += "--allow-secret-rotation" }

$quotedRepoPath = Quote-Sh $RepoPath
$quotedEnvPath = Quote-Sh $EnvPath
$command = $argsList -join " "

$remoteScript = @"
set -eu
cd $quotedRepoPath
export PYTHONPATH="src:`${PYTHONPATH:-}"
set -a
. $quotedEnvPath
set +a
token_file=/tmp/agentic-mesh-v3-graph-token.json
cleanup() {
  rm -f "$token_file"
}
trap cleanup EXIT
python3 - <<'PY'
import json
import os
from pathlib import Path

token = os.environ.get("AGENTIC_MESH_TEAMS_TOKEN", "").strip()
if not token:
    raise SystemExit("AGENTIC_MESH_TEAMS_TOKEN is missing from $EnvPath")

Path("/tmp/agentic-mesh-v3-graph-token.json").write_text(
    json.dumps({"access_token": token}),
    encoding="utf-8",
)
os.chmod("/tmp/agentic-mesh-v3-graph-token.json", 0o600)
PY
$command
"@

$remoteScript = $remoteScript -replace "`r`n", "`n"
if (-not $remoteScript.EndsWith("`n")) {
    $remoteScript += "`n"
}

$remoteScript | ssh $LinuxHost "bash -s"
