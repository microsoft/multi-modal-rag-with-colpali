#!/usr/bin/env pwsh
# ColPali Endpoint Deployment Script
# Deploys registered ColPali models to Azure ML Online Endpoint
# Usage: .\deploy_endpoint.ps1

$ErrorActionPreference = "Stop"

# Get the project root directory (go up two levels: scripts/windows -> scripts -> root)
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ColpaliModulePath = Join-Path $ProjectRoot "modules\colpali"

Write-Host "Project root: $ProjectRoot"
Write-Host "ColPali module path: $ColpaliModulePath"

# Change to the colpali module directory
Set-Location $ColpaliModulePath

# Run the endpoint deployment
Write-Host "Deploying ColPali models to Azure ML Online Endpoint..."
Write-Host "This will deploy the registered models to the inference endpoint created by Bicep."

uv run scripts\deploy_endpoint.py

if ($LASTEXITCODE -ne 0) {
    throw "Endpoint deployment failed"
}

Write-Host "Endpoint deployment completed successfully!"
Write-Host "ColPali models are now available for inference via the online endpoint."
