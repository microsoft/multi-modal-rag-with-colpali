#!/usr/bin/env pwsh
# Complete deployment script: deploys Bicep infrastructure
# Usage: .\deploy_infra.ps1 [-ResourceGroup <resource-group>] [-DeployRoles <true|false>]
# Note: AKS and Event Grid always deploy (containers pushed later via Helm)
# Note: baseName and location are defined in infra/src/main.bicepparam

[CmdletBinding()]
param(
    [Parameter()]
    [string]$ResourceGroup = "colpali-gpu-rg",

    [Parameter()]
    [string]$DeployRoles = "true"
)

$ErrorActionPreference = "Stop"

# Get the absolute path of the script directory
$ScriptDir = $PSScriptRoot
# Get the absolute path of the project root (go up two levels: scripts/windows -> scripts -> root)
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
# Change to the project root directory
Set-Location $ProjectRoot

Write-Host "Project root: $ProjectRoot"

# Get user object ID
Write-Host "Getting user object ID..."
$UserObjectId = az ad signed-in-user show --query id -o tsv
if ($LASTEXITCODE -ne 0) {
    throw "Failed to get user object ID"
}

# Use absolute paths for the Bicep files
$BicepParamFile = Join-Path $ProjectRoot "infra\src\main.bicepparam"
Write-Host "Using Bicep parameter file: $BicepParamFile"

# Deploy resources with Bicep using the parameter file
Write-Host "Deploying resources with Bicep..."
Write-Host "Resource Group: '$ResourceGroup'"
Write-Host "Bicep Param File: '$BicepParamFile'"
Write-Host "User Object ID: '$UserObjectId'"
Write-Host "Deploy Roles: '$DeployRoles'"

$deploymentOutput = az deployment group create --resource-group "$ResourceGroup" --parameters "$BicepParamFile" userObjectId="$UserObjectId" deployRoleAssignments="$DeployRoles" --query "properties.outputs" -o json | ConvertFrom-Json

if ($LASTEXITCODE -ne 0) {
    throw "Bicep deployment failed"
}

# Convert all Bicep outputs to environment variables automatically
$envValues = @{}
foreach ($output in $deploymentOutput.PSObject.Properties) {
    $outputName = $output.Name
    $outputValue = $output.Value.value

    # Convert camelCase to UPPER_SNAKE_CASE for env var naming
    $envVarName = $outputName -creplace '([a-z])([A-Z])', '$1_$2' -replace '^([a-z])', { $_.Groups[1].Value.ToUpper() }
    $envVarName = $envVarName.ToUpper()

    $envValues[$envVarName] = $outputValue
}

# Preserve all existing environment variables from .env file
$EnvFile = Join-Path $ProjectRoot ".env"
if (Test-Path $EnvFile) {
    Write-Host "Preserving existing environment variables from .env file..."
    $existingContent = Get-Content $EnvFile
    $preservedCount = 0
    foreach ($line in $existingContent) {
        if ($line -match '^([^#][^=]*?)=(.*)$') {
            $key = $matches[1]
            $value = $matches[2]
            # Only preserve if not already set by Bicep outputs (Bicep takes precedence)
            if (-not $envValues.ContainsKey($key)) {
                $envValues[$key] = $value
                $preservedCount++
            }
        }
    }
    if ($preservedCount -gt 0) {
        Write-Host "  Preserved $preservedCount existing environment variables"
    }
}

# Create .env file in project root - automatically generate from envValues hashtable
Write-Host "Creating .env file at $EnvFile"

$envLines = @()
foreach ($key in ($envValues.Keys | Sort-Object)) {
    $value = $envValues[$key]
    if ($value -ne $null -and $value -ne "") {
        $envLines += "$key=$value"
    }
}

$envContent = $envLines -join "`n"
Set-Content -Path $EnvFile -Value $envContent -NoNewline

Write-Host "`nInfrastructure deployment complete!"
