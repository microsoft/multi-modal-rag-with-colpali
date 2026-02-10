#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation. Licensed under the MIT License.
# Deploys Bicep infrastructure. Usage: .\deploy_infra.ps1 [-ResourceGroup <rg>] [-DeployRoles <true|false>]

param([string]$ResourceGroup = "colpali-rg", [string]$DeployRoles = "true")

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$UserObjectId = az ad signed-in-user show --query id -o tsv
if ($LASTEXITCODE -ne 0) { throw "Failed to get user object ID" }

Write-Host "Deploying infrastructure to $ResourceGroup..."
$deploymentOutput = az deployment group create --resource-group $ResourceGroup `
    --parameters "$ProjectRoot\modules\infra\src\main.bicepparam" `
    userObjectId=$UserObjectId deployRoleAssignments=$DeployRoles `
    --query "properties.outputs" -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw "Bicep deployment failed" }

# Convert outputs to env vars (camelCase -> UPPER_SNAKE_CASE)
$envValues = @{}
foreach ($output in $deploymentOutput.PSObject.Properties) {
    $envVarName = ($output.Name -creplace '([a-z])([A-Z])', '$1_$2').ToUpper()
    $envValues[$envVarName] = $output.Value.value
}

# Preserve existing .env values not in deployment output
$EnvFile = "$ProjectRoot\.env"
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        if ($_ -match '^([^#][^=]*?)=(.*)$' -and -not $envValues.ContainsKey($matches[1])) {
            $envValues[$matches[1]] = $matches[2]
        }
    }
}

# Write .env file
($envValues.Keys | Sort-Object | Where-Object { $envValues[$_] } | ForEach-Object { "$_=$($envValues[$_])" }) -join "`n" | Set-Content $EnvFile -NoNewline

# Bootstrap K8s cluster
$AksClusterName = $envValues["AKS_CLUSTER_NAME"]
if ($AksClusterName) {
    Write-Host "Bootstrapping AKS cluster $AksClusterName..."
    az aks get-credentials --resource-group $ResourceGroup --name $AksClusterName --overwrite-existing
    if ($LASTEXITCODE -ne 0) { throw "Failed to get AKS credentials" }

    $K8sDir = "$ProjectRoot\modules\infra\src\k8s"
    @("namespace-spot-tolerations.yaml", "gpu-resources-namespace.yaml", "nvidia-device-plugin.yaml", "dcgm-exporter.yaml") | ForEach-Object { kubectl apply -f "$K8sDir\$_" }
}

Write-Host "Infrastructure deployment complete!"
