param(
    [string]$VMName = "linuxch",
    [int64]$AgenticMeshDiskSizeGB = 500,
    [int64]$OsDiskAdditionalSizeGB = 250,
    [string]$AgenticMeshDiskName = "linuxch-agentic-mesh-500gb.vhdx",
    [string]$DiskDirectory
)

$ErrorActionPreference = "Stop"

function Require-Admin {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated PowerShell session on the Hyper-V host."
    }
}

function Format-Bytes([int64]$bytes) {
    "{0:N1} GB" -f ($bytes / 1GB)
}

Require-Admin

$vm = Get-VM -Name $VMName
$drives = @(Get-VMHardDiskDrive -VMName $VMName | Sort-Object ControllerType, ControllerNumber, ControllerLocation)
if ($drives.Count -eq 0) {
    throw "VM '$VMName' has no hard disk drives."
}

$osDrive = $drives[0]
$osVhd = Get-VHD -Path $osDrive.Path
$targetOsSize = [int64]($osVhd.Size + ($OsDiskAdditionalSizeGB * 1GB))

Write-Host "VM: $($vm.Name) ($($vm.State))"
Write-Host "Assuming OS disk is first attached disk:"
Write-Host "  $($osDrive.Path)"
Write-Host "  current size: $(Format-Bytes $osVhd.Size)"
Write-Host "  target size:  $(Format-Bytes $targetOsSize)"

if ($osVhd.Size -lt $targetOsSize) {
    Resize-VHD -Path $osDrive.Path -SizeBytes $targetOsSize
    Write-Host "Expanded OS VHD by $OsDiskAdditionalSizeGB GB."
} else {
    Write-Host "OS VHD is already at or above requested target size; no resize needed."
}

if (-not $DiskDirectory) {
    $DiskDirectory = Split-Path -Parent $osDrive.Path
}

if (-not (Test-Path -LiteralPath $DiskDirectory)) {
    New-Item -ItemType Directory -Path $DiskDirectory | Out-Null
}

$agenticMeshDiskPath = Join-Path $DiskDirectory $AgenticMeshDiskName
$agenticMeshSizeBytes = [int64]($AgenticMeshDiskSizeGB * 1GB)

if (-not (Test-Path -LiteralPath $agenticMeshDiskPath)) {
    New-VHD -Path $agenticMeshDiskPath -SizeBytes $agenticMeshSizeBytes -Dynamic | Out-Null
    Write-Host "Created Agentic Mesh data VHD: $agenticMeshDiskPath ($(Format-Bytes $agenticMeshSizeBytes))."
} else {
    $existing = Get-VHD -Path $agenticMeshDiskPath
    if ($existing.Size -lt $agenticMeshSizeBytes) {
        Resize-VHD -Path $agenticMeshDiskPath -SizeBytes $agenticMeshSizeBytes
        Write-Host "Expanded existing Agentic Mesh data VHD to $(Format-Bytes $agenticMeshSizeBytes)."
    } else {
        Write-Host "Agentic Mesh data VHD already exists at requested size or larger."
    }
}

$attachedPaths = @(Get-VMHardDiskDrive -VMName $VMName | ForEach-Object { $_.Path })
if ($attachedPaths -notcontains $agenticMeshDiskPath) {
    Add-VMHardDiskDrive -VMName $VMName -ControllerType SCSI -Path $agenticMeshDiskPath
    Write-Host "Attached Agentic Mesh data VHD to $VMName."
} else {
    Write-Host "Agentic Mesh data VHD is already attached to $VMName."
}

Write-Host ""
Write-Host "Next inside linuxch:"
Write-Host "  sh /home/nich/agentic-mesh-system-clean/scripts/linuxch-migrate-agentic-mesh-storage.sh"
