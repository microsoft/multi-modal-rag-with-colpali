#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation. Licensed under the MIT License.
# Deploys Bicep infrastructure. Usage: .\deploy_infra.ps1 [-ResourceGroup <rg>] [-DeployRoles <true|false>] [-Subscription <name-or-id>]

param([string]$ResourceGroup = "colpali-rg", [string]$DeployRoles = "true", [string]$Subscription)

$ErrorActionPreference = "Stop"

function Get-AzureDeploymentContext {
    param([string]$Subscription)

    $accountArguments = @("account", "show", "--output", "json", "--only-show-errors")
    if ($Subscription) { $accountArguments += @("--subscription", $Subscription) }
    $accountJson = az @accountArguments
    if ($LASTEXITCODE -ne 0) { throw "Failed to resolve Azure subscription. Run az login and select a subscription." }
    $account = $accountJson | ConvertFrom-Json
    if (-not $account.id -or -not $account.tenantId) { throw "Azure subscription or tenant ID is missing." }
    if ($account.user.type -ne "user") { throw "This script requires an interactive Azure user login for its user role assignments." }

    $tokenJson = az account get-access-token --subscription $account.id --resource-type arm --output json --only-show-errors
    if ($LASTEXITCODE -ne 0) { throw "Failed to obtain an ARM token for the selected subscription." }
    $token = $tokenJson | ConvertFrom-Json
    $segments = ([string]$token.accessToken).Split(".")
    if ($segments.Count -ne 3) { throw "Cannot resolve the user object ID: the ARM token is not a JWT." }

    # A guest user's object ID is tenant-specific; a default-directory Graph lookup can return the wrong identity.
    $payload = $segments[1].Replace("-", "+").Replace("_", "/")
    $payload = $payload.PadRight([int]([Math]::Ceiling($payload.Length / 4.0) * 4), "=")
    $claims = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($payload)) | ConvertFrom-Json
    if ($token.subscription -ne $account.id -or $token.tenant -ne $account.tenantId -or $claims.tid -ne $account.tenantId) {
        throw "The ARM token does not match the selected subscription and tenant."
    }
    $objectId = [guid]::Empty
    if (-not [guid]::TryParse([string]$claims.oid, [ref]$objectId) -or $objectId -eq [guid]::Empty) {
        throw "The ARM token does not contain a valid user object ID."
    }

    [pscustomobject]@{
        SubscriptionId = $account.id
        TenantId = $account.tenantId
        UserObjectId = $objectId.ToString()
    }
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$context = Get-AzureDeploymentContext -Subscription $Subscription
$UserObjectId = $context.UserObjectId

Write-Host "Deploying infrastructure to $ResourceGroup..."
$deploymentOutput = az deployment group create --subscription $context.SubscriptionId --resource-group $ResourceGroup `
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
    az aks get-credentials --subscription $context.SubscriptionId --resource-group $ResourceGroup --name $AksClusterName --overwrite-existing
    if ($LASTEXITCODE -ne 0) { throw "Failed to get AKS credentials" }

    $K8sDir = "$ProjectRoot\modules\infra\src\k8s"
    @("namespace-spot-tolerations.yaml", "gpu-resources-namespace.yaml", "nvidia-device-plugin.yaml", "dcgm-exporter.yaml") | ForEach-Object {
        kubectl apply -f "$K8sDir\$_"
        if ($LASTEXITCODE -ne 0) { throw "Failed to apply Kubernetes manifest $_" }
    }
}

Write-Host "Infrastructure deployment complete!"
