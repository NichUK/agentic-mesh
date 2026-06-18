param(
    [string]$LinuxHost = "nich@linuxch",
    [string]$RepoPath = "/home/nich/agentic-mesh",
    [string]$EnvPath = "examples/projects/agentic-mesh-dev/deploy/compose/.env",
    [string]$DriveId = "",
    [string]$SponsorTeamsUserId = "",
    [string]$TeamsSenderUserId = "",
    [string]$GraphClientId = $env:AGENTIC_MESH_GRAPH_CLIENT_ID,
    [string]$TenantId = $env:AGENTIC_MESH_GRAPH_TENANT_ID,
    [switch]$SkipDeviceLogin
)

$ErrorActionPreference = "Stop"

$scopes = @(
    "https://graph.microsoft.com/Files.ReadWrite.All",
    "https://graph.microsoft.com/Chat.Create",
    "https://graph.microsoft.com/Chat.ReadWrite",
    "https://graph.microsoft.com/ChatMessage.Send",
    "https://graph.microsoft.com/ChannelMessage.Send",
    "https://graph.microsoft.com/AppCatalog.ReadWrite.All",
    "https://graph.microsoft.com/Team.ReadBasic.All",
    "https://graph.microsoft.com/Channel.ReadBasic.All",
    "https://graph.microsoft.com/Group.Read.All",
    "https://graph.microsoft.com/TeamsAppInstallation.ReadWriteForTeam",
    "https://graph.microsoft.com/TeamsAppInstallation.ReadWriteForUser",
    "https://graph.microsoft.com/TeamsAppInstallation.ReadForUser",
    "https://graph.microsoft.com/Application.ReadWrite.All",
    "offline_access"
)

function Get-ScopedGraphToken {
    $previousErrorActionPreference = $ErrorActionPreference
    $previousNativeErrorActionPreference = $null
    $hasNativeErrorActionPreference = Test-Path Variable:\PSNativeCommandUseErrorActionPreference
    if ($hasNativeErrorActionPreference) {
        $previousNativeErrorActionPreference = $PSNativeCommandUseErrorActionPreference
        $script:PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $ErrorActionPreference = "Continue"
        $tokenOutput = (& az account get-access-token --scope $scopes --query accessToken -o tsv 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $tokenOutput) {
            return $null
        }
        return $tokenOutput
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        if ($hasNativeErrorActionPreference) {
            $script:PSNativeCommandUseErrorActionPreference = $previousNativeErrorActionPreference
        }
    }
}

function Get-DefaultTenantId {
    $previousErrorActionPreference = $ErrorActionPreference
    $previousNativeErrorActionPreference = $null
    $hasNativeErrorActionPreference = Test-Path Variable:\PSNativeCommandUseErrorActionPreference
    if ($hasNativeErrorActionPreference) {
        $previousNativeErrorActionPreference = $PSNativeCommandUseErrorActionPreference
        $script:PSNativeCommandUseErrorActionPreference = $false
    }
    try {
        $ErrorActionPreference = "Continue"
        $tenantOutput = (& az account show --query tenantId -o tsv 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $tenantOutput) {
            return "organizations"
        }
        return $tenantOutput
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        if ($hasNativeErrorActionPreference) {
            $script:PSNativeCommandUseErrorActionPreference = $previousNativeErrorActionPreference
        }
    }
}

function Get-DeviceCodeGraphToken {
    param(
        [Parameter(Mandatory = $true)][string]$ClientId,
        [Parameter(Mandatory = $true)][string]$Tenant,
        [Parameter(Mandatory = $true)][string[]]$Scopes
    )

    $scopeText = ($Scopes -join " ")
    $deviceCodeUri = "https://login.microsoftonline.com/$Tenant/oauth2/v2.0/devicecode"
    $tokenUri = "https://login.microsoftonline.com/$Tenant/oauth2/v2.0/token"
    $deviceResponse = Invoke-RestMethod `
        -Method Post `
        -Uri $deviceCodeUri `
        -ContentType "application/x-www-form-urlencoded" `
        -ErrorAction Stop `
        -Body @{
            client_id = $ClientId
            scope = $scopeText
        }

    Write-Host $deviceResponse.message

    $deadline = (Get-Date).AddSeconds([int]$deviceResponse.expires_in)
    $intervalSeconds = [Math]::Max([int]$deviceResponse.interval, 5)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds $intervalSeconds
        try {
            $tokenResponse = Invoke-RestMethod `
                -Method Post `
                -Uri $tokenUri `
                -ContentType "application/x-www-form-urlencoded" `
                -ErrorAction Stop `
                -Body @{
                    grant_type = "urn:ietf:params:oauth:grant-type:device_code"
                    client_id = $ClientId
                    device_code = $deviceResponse.device_code
                }
            return $tokenResponse
        }
        catch {
            $errorBody = $null
            if ($_.ErrorDetails -and $_.ErrorDetails.Message) {
                $errorBody = $_.ErrorDetails.Message
            }
            elseif ($_.Exception.Response -and $_.Exception.Response.GetResponseStream()) {
                $reader = [System.IO.StreamReader]::new($_.Exception.Response.GetResponseStream())
                $errorBody = $reader.ReadToEnd()
            }
            $errorCode = ""
            if ($errorBody) {
                try {
                    $errorCode = (ConvertFrom-Json $errorBody).error
                }
                catch {
                    $errorCode = ""
                }
            }
            if ($errorCode -eq "authorization_pending") {
                continue
            }
            if ($errorCode -eq "slow_down") {
                $intervalSeconds += 5
                continue
            }
            if ($errorCode -eq "authorization_declined") {
                throw "Device-code login was declined."
            }
            if ($errorCode -eq "expired_token") {
                throw "Device-code login expired before completion."
            }
            if ($errorBody) {
                throw "Device-code token request failed: $errorBody"
            }
            throw
        }
    }
    throw "Device-code login expired before completion."
}

$deviceTokenResponse = $null
$token = Get-ScopedGraphToken
if (-not $token -and -not $SkipDeviceLogin) {
    if (-not $GraphClientId) {
        throw "AGENTIC_MESH_GRAPH_CLIENT_ID or -GraphClientId is required. Azure CLI's first-party client cannot request the required Graph scopes."
    }
    if (-not $TenantId) {
        $TenantId = Get-DefaultTenantId
    }
    Write-Host "Scoped Graph token is not available. Starting Agentic Mesh device-code login..."
    $deviceTokenResponse = Get-DeviceCodeGraphToken -ClientId $GraphClientId -Tenant $TenantId -Scopes $scopes
    $token = $deviceTokenResponse.access_token
}
if (-not $token) {
    throw "Azure CLI did not return a scoped Graph access token. Run this script with -GraphClientId or set AGENTIC_MESH_GRAPH_CLIENT_ID to use Agentic Mesh device-code login."
}

$updates = @{
    AGENTIC_MESH_ONEDRIVE_TOKEN = $token
    AGENTIC_MESH_TEAMS_TOKEN = $token
    AGENTIC_MESH_GRAPH_SCOPES = ($scopes -join " ")
}

if ($GraphClientId) {
    $updates.AGENTIC_MESH_GRAPH_CLIENT_ID = $GraphClientId
}
if ($TenantId) {
    $updates.AGENTIC_MESH_GRAPH_TENANT_ID = $TenantId
}
if ($deviceTokenResponse -and $deviceTokenResponse.refresh_token) {
    $updates.AGENTIC_MESH_GRAPH_REFRESH_TOKEN = $deviceTokenResponse.refresh_token
}

if ($DriveId) {
    $updates.AGENTIC_MESH_ONEDRIVE_DRIVE_ID = $DriveId
}
if ($SponsorTeamsUserId) {
    $updates.AGENTIC_MESH_SPONSOR_TEAMS_USER_ID = $SponsorTeamsUserId
}
if ($TeamsSenderUserId) {
    $updates.AGENTIC_MESH_TEAMS_SENDER_USER_ID = $TeamsSenderUserId
}

$payload = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($updates | ConvertTo-Json -Compress)))

function Quote-Sh([string]$Value) {
    return "'" + ($Value -replace "'", "'\\''") + "'"
}

$quotedRepoPath = Quote-Sh $RepoPath
$quotedEnvPath = Quote-Sh $EnvPath

$remoteScript = @"
set -eu
repo_path=$quotedRepoPath
env_path=$quotedEnvPath
payload_b64='$payload'
export env_path payload_b64
cd "`$repo_path"
python3 - <<'PY'
import base64
import json
import os
import shlex
from pathlib import Path

env_path = Path(os.environ["env_path"])
payload = json.loads(base64.b64decode(os.environ["payload_b64"]).decode("utf-8"))
example_path = env_path.with_name(".env.example")

if env_path.exists():
    lines = env_path.read_text(encoding="utf-8").splitlines()
elif example_path.exists():
    lines = example_path.read_text(encoding="utf-8").splitlines()
else:
    lines = []

seen = set()
updated_lines = []
def env_line(key, value):
    return f"{key}={shlex.quote(str(value))}"

for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        updated_lines.append(line)
        continue
    key, _value = line.split("=", 1)
    if key in payload:
        updated_lines.append(env_line(key, payload[key]))
        seen.add(key)
    else:
        updated_lines.append(line)

for key in sorted(set(payload) - seen):
    updated_lines.append(env_line(key, payload[key]))

tmp_path = env_path.with_suffix(env_path.suffix + ".tmp")
tmp_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
os.chmod(tmp_path, 0o600)
tmp_path.replace(env_path)
PY
"@
$remoteScript = $remoteScript -replace "`r", ""
if (-not $remoteScript.EndsWith("`n")) {
    $remoteScript += "`n"
}

$env:payload_b64 = $payload
$env:env_path = $EnvPath
try {
    $remoteScript | ssh $LinuxHost "bash -s"
}
finally {
    Remove-Item Env:\payload_b64 -ErrorAction SilentlyContinue
    Remove-Item Env:\env_path -ErrorAction SilentlyContinue
}

Write-Host "Updated V3 Graph tokens in $LinuxHost`:$RepoPath/$EnvPath"
Write-Host "Next: ssh $LinuxHost 'cd $RepoPath && sh scripts/release-linuxch-compose.sh'"
