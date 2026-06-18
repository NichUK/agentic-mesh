param(
    [string]$LinuxHost = "nich@linuxch",
    [string]$RepoPath = "/home/nich/agentic-mesh",
    [string]$ProjectFile = "examples/projects/agentic-mesh-dev/agentic-mesh/project-v3.yaml",
    [string]$OrganizationFile = "config/organization.yaml",
    [string]$EnvPath = "examples/projects/agentic-mesh-dev/deploy/compose/.env",
    [string]$ComposeFile = "examples/projects/agentic-mesh-dev/deploy/compose/docker-compose.yml",
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

function Convert-ToContainerPath([string]$Path) {
    $normalized = $Path -replace "\\", "/"
    $projectPrefix = "examples/projects/agentic-mesh-dev/"
    if ($normalized.StartsWith($projectPrefix)) {
        return "/mesh/project/" + $normalized.Substring($projectPrefix.Length)
    }
    if ($normalized.StartsWith("config/") -or $normalized.StartsWith("src/") -or $normalized.StartsWith("scripts/")) {
        return "/mesh/system/" + $normalized
    }
    return $normalized
}

$containerProjectFile = Convert-ToContainerPath $ProjectFile
$containerOrganizationFile = Convert-ToContainerPath $OrganizationFile
$containerTeamsAppPackageRoot = Convert-ToContainerPath $TeamsAppPackageRoot

$argsList = @(
    "python", "-m", "agentic_mesh_v3.cli", "install-project",
    "--project-file", (Quote-Sh $containerProjectFile),
    "--organization-file", (Quote-Sh $containerOrganizationFile),
    "--graph-token-file", "/tmp/agentic-mesh-v3-graph-token.json",
    "--teams-app-package-root", (Quote-Sh $containerTeamsAppPackageRoot)
)
if ($Apply) { $argsList += "--apply" }
if ($AllowCreateTeam) { $argsList += "--allow-create-team" }
if ($AllowCreateChannel) { $argsList += "--allow-create-channel" }
if ($AllowRegisterApps) { $argsList += "--allow-register-apps" }
if ($AllowInstallApps) { $argsList += "--allow-install-apps" }
if ($AllowUninstallStale) { $argsList += "--allow-uninstall-stale" }
if ($AllowSecretRotation) { $argsList += "--allow-secret-rotation" }

$quotedRepoPath = Quote-Sh $RepoPath
$quotedComposeFile = Quote-Sh $ComposeFile
$command = $argsList -join " "

$remoteScript = @"
set -eu
cd $quotedRepoPath
docker compose --profile v3 -f $quotedComposeFile run --rm --no-deps v3-runtime sh -lc '
set -eu
export PYTHONPATH="/mesh/system/src:`${PYTHONPATH:-}"
token_file=/tmp/agentic-mesh-v3-graph-token.json
cleanup() {
  rm -f "`$token_file"
}
trap cleanup EXIT
printf "{\"access_token\":\"%s\"}" "`$AGENTIC_MESH_TEAMS_TOKEN" > "`$token_file"
chmod 600 "`$token_file"
$command
'
"@

$remoteScript = $remoteScript -replace "`r", ""
if (-not $remoteScript.EndsWith("`n")) {
    $remoteScript += "`n"
}

$remoteScript | ssh $LinuxHost "bash -s"
