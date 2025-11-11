#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

# Build and push ColQwen inference and downloader containers

$ErrorActionPreference = "Stop"

Write-Host "Building ColQwen Containers"

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

$colqwenDir = Join-Path $PSScriptRoot "..\..\modules\colqwen"
Push-Location $colqwenDir

try {
    # Login to ACR
    Write-Host "Logging into Azure Container Registry: $acrName"
    az acr login --name $acrName
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to login to ACR"
    }

    # Build and push image remotely with cache
    Write-Host "Building ColQwen unified image remotely with cache..."
    $inferenceImage = "$acrName.azurecr.io/colqwen-inference:$imageTag"

    az acr build --registry $acrName --image $inferenceImage --file Dockerfile .

    Write-Host "ColQwen image built and pushed successfully!"
    Write-Host "   Image: $inferenceImage"
    Write-Host "   Tag: $imageTag"

    # Update .env file with the image tag
    $envContent = Get-Content $EnvFile
    $newEnvContent = @()
    $tagUpdated = $false

    foreach ($line in $envContent) {
        if ($line -match '^COLQWEN_IMAGE_TAG=') {
            $newEnvContent += "COLQWEN_IMAGE_TAG=$imageTag"
            $tagUpdated = $true
        }
        else {
            $newEnvContent += $line
        }
    }

    # Add COLQWEN_IMAGE_TAG if it doesn't exist
    if (-not $tagUpdated) {
        $newEnvContent += "COLQWEN_IMAGE_TAG=$imageTag"
    }

    Set-Content $EnvFile $newEnvContent
    Write-Host "Updated .env with COLQWEN_IMAGE_TAG=$imageTag"

    Write-Host "ColQwen container build completed successfully!"
    Write-Host "Run 'apply_helm.ps1' to deploy the updated image to AKS"

}
catch {
    Write-Host "Error building ColQwen images: $($_.Exception.Message)"
    throw
}
finally {
    Pop-Location
}
