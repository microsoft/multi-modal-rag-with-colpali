#!/usr/bin/env pwsh
# ColPali Model Registration Script
# Downloads and registers ColPali models in Azure ML
# Usage: .\register_model.ps1

$ErrorActionPreference = "Stop"

# Get the project root directory (go up two levels: scripts/windows -> scripts -> root)
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$ColpaliModulePath = Join-Path $ProjectRoot "modules\colpali"

Write-Host "Project root: $ProjectRoot"
Write-Host "ColPali module path: $ColpaliModulePath"

# Change to the colpali module directory
Set-Location $ColpaliModulePath

# Run the model registration pipeline
Write-Host "Running ColPali model registration pipeline..."
Write-Host "This will download ColPali models from HuggingFace and register them in Azure ML."

uv run pipeline.py

if ($LASTEXITCODE -ne 0) {
    throw "Pipeline execution failed"
}

Write-Host "Pipeline completed successfully!"
Write-Host "ColPali models have been downloaded and registered in Azure ML."
