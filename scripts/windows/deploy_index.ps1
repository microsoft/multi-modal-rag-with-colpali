#!/usr/bin/env pwsh
# ColPali Index Deployment Script (AI Search + QDRANT)
# Usage: .\deploy_index.ps1

# Get the project root directory
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$IndexModulePath = Join-Path $ProjectRoot "modules\index"

# Change to the index module directory and run with UV
Set-Location $IndexModulePath

# Run the unified deployment script
Write-Host "Deploying QDRANT Collection..."
uv run scripts\deploy_qdrant.py
