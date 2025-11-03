#!/usr/bin/env pwsh

# Build and push document processor container

$ErrorActionPreference = "Stop"

Write-Host "Building Document Processor Container"

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

# Navigate to document processor directory
$docProcessorDir = Join-Path $PSScriptRoot "..\..\modules\document_processor"

Set-Location $docProcessorDir

# Build and push container image
Write-Host "Building and pushing to $acrName..."

az acr build --registry $acrName --image "document-processor:latest" .

if ($LASTEXITCODE -eq 0) {
    Write-Host "Container built and pushed successfully"
} else {
    Write-Host "Build failed"
    throw "Build failed"
}
