#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

# Universal container build script
# Parameters:
#   -ServiceName: Display name for the service being built
#   -ImageName: Name for the Docker image (e.g., "colqwen-inference")
#   -Directory: Relative path to the module directory (e.g., "modules\colqwen_inference")
#   -Dockerfile: Name of the Dockerfile (default: "Dockerfile")
#   -EnvVarName: Name of the environment variable to update (e.g., "COLQWEN_INFERENCE_IMAGE_TAG")

param(
    [Parameter(Mandatory = $true)]
    [string]$ServiceName,

    [Parameter(Mandatory = $true)]
    [string]$ImageName,

    [Parameter(Mandatory = $true)]
    [string]$Directory,

    [Parameter(Mandatory = $false)]
    [string]$Dockerfile = "Dockerfile",

    [Parameter(Mandatory = $true)]
    [string]$EnvVarName
)

$ErrorActionPreference = "Stop"

Write-Host "Building $ServiceName Container"

# Load .env file
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$EnvFile = Join-Path $ProjectRoot ".env"

if (-not (Test-Path $EnvFile)) {
    Write-Host ".env file not found at $EnvFile"
    throw ".env file not found. Run deploy_infra.ps1 first."
}

# Parse .env file
$envVars = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^([^#][^=]*?)=(.*)$') {
        $envVars[$matches[1]] = $matches[2]
    }
}

# Get ACR name from .env
$acrName = $envVars['ACR_NAME']
if (-not $acrName) {
    Write-Host "ACR_NAME not found in .env file"
    throw "ACR_NAME not found in .env file"
}

# Generate unique tag using git commit hash
try {
    $imageTag = (git rev-parse --short HEAD 2>$null)
    Write-Host "Generated image tag from git hash: $imageTag"
}
catch {
    Write-Host "Git not available, using timestamp as fallback"
    $imageTag = (Get-Date -Format "yyyyMMdd-HHmmss")
}

# Navigate to the specified directory
$targetDir = Join-Path $PSScriptRoot "..\..\$Directory"
Push-Location $targetDir

try {
    # Login to ACR
    Write-Host "Logging into Azure Container Registry: $acrName"
    az acr login --name $acrName
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to login to ACR"
    }

    # Build and push image remotely with cache
    Write-Host "Building $ServiceName image remotely with cache..."
    $fullImageName = "$acrName.azurecr.io/$ImageName`:$imageTag"

    az acr build --registry $acrName --image $fullImageName --file $Dockerfile .

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to build $ServiceName image"
    }

    Write-Host "$ServiceName image built and pushed successfully!"
    Write-Host "   Image: $fullImageName"
    Write-Host "   Tag: $imageTag"

    # Update .env file with the image tag
    $envContent = Get-Content $EnvFile
    $newEnvContent = @()
    $tagUpdated = $false

    foreach ($line in $envContent) {
        if ($line -match "^$EnvVarName=") {
            $newEnvContent += "$EnvVarName=$imageTag"
            $tagUpdated = $true
        }
        else {
            $newEnvContent += $line
        }
    }

    # Add environment variable if it doesn't exist
    if (-not $tagUpdated) {
        $newEnvContent += "$EnvVarName=$imageTag"
    }

    Set-Content $EnvFile $newEnvContent
    Write-Host "Updated .env with $EnvVarName=$imageTag"

    Write-Host "$ServiceName container build completed successfully!"
    Write-Host "Run 'apply_helm.ps1' to deploy the updated image to AKS"

}
catch {
    Write-Host "Error building $ServiceName image: $($_.Exception.Message)"
    throw
}
finally {
    Pop-Location
}
