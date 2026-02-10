#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation. Licensed under the MIT License.
# Builds a container image. Usage: .\build_container.ps1 -ServiceName <name> -ImageName <image> -Directory <dir> -EnvVarName <var>

param(
    [Parameter(Mandatory)][string]$ServiceName,
    [Parameter(Mandatory)][string]$ImageName,
    [Parameter(Mandatory)][string]$Directory,
    [string]$Dockerfile = "Dockerfile",
    [Parameter(Mandatory)][string]$EnvVarName
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = "$ProjectRoot\.env"

if (-not (Test-Path $EnvFile)) { throw ".env file not found. Run deploy_infra.ps1 first." }

$envVars = @{}
Get-Content $EnvFile | ForEach-Object { if ($_ -match '^([^#][^=]*?)=(.*)$') { $envVars[$matches[1]] = $matches[2] } }

$acrName = $envVars['ACR_NAME']
if (-not $acrName) { throw "ACR_NAME not found in .env" }

$imageTag = try { git rev-parse --short HEAD 2>$null } catch { Get-Date -Format "yyyyMMdd-HHmmss" }

Push-Location "$PSScriptRoot\..\$Directory"
try {
    az acr login --name $acrName
    if ($LASTEXITCODE -ne 0) { throw "Failed to login to ACR" }

    Write-Host "Building $ServiceName ($imageTag)..."
    az acr build --registry $acrName --image "$acrName.azurecr.io/${ImageName}:$imageTag" --file $Dockerfile .
    if ($LASTEXITCODE -ne 0) { throw "Failed to build $ServiceName" }

    # Update .env with new tag
    $envContent = Get-Content $EnvFile
    $found = $false
    $newContent = $envContent | ForEach-Object {
        if ($_ -match "^$EnvVarName=") { $found = $true; "$EnvVarName=$imageTag" } else { $_ }
    }
    if (-not $found) { $newContent += "$EnvVarName=$imageTag" }
    Set-Content $EnvFile $newContent

    Write-Host "$ServiceName built: $imageTag"
} finally { Pop-Location }
