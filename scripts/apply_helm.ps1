#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation. Licensed under the MIT License.
# Deploys ColPali stack to AKS. Usage: .\apply_helm.ps1 [-LocalAccess] [-Help]

param([switch]$LocalAccess, [Alias("h")][switch]$Help)

if ($Help) {
    Write-Host "Usage: apply_helm.ps1 [-LocalAccess]"
    Write-Host "  -LocalAccess  Enable ingress for all services (default: Agent UI only)"
    exit 0
}

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$EnvFile = "$ProjectRoot\.env"
if (-not (Test-Path $EnvFile)) { throw ".env file not found. Run deploy_infra.ps1 first." }

$env = @{}
Get-Content $EnvFile | ForEach-Object { if ($_ -match '^([^=]+)=(.*)$') { $env[$matches[1]] = $matches[2] } }

# Validate required config
@('RESOURCE_GROUP','AKS_CLUSTER_NAME','ACR_LOGIN_SERVER','WORKLOAD_IDENTITY_CLIENT_ID','AKS_KUBELET_IDENTITY_CLIENT_ID',
  'DATA_STORAGE_ACCOUNT_NAME','SERVICE_BUS_NAMESPACE_NAME','SERVICE_BUS_QUEUE_NAME','KEY_VAULT_NAME',
  'QDRANT_API_KEY_SECRET_NAME','QDRANT_READ_ONLY_API_KEY_SECRET_NAME','APPLICATION_INSIGHTS_CONNECTION_STRING_SECRET_NAME') | ForEach-Object {
    if (-not $env[$_]) { throw "Missing required config: $_" }
}

Write-Host "Deploying to $($env['AKS_CLUSTER_NAME'])..."
az aks get-credentials --resource-group $env['RESOURCE_GROUP'] --name $env['AKS_CLUSTER_NAME'] --overwrite-existing
if ($LASTEXITCODE -ne 0) { throw "Failed to get AKS credentials" }

Push-Location "modules/helm/colpali-stack"
helm dependency update
Pop-Location

$ingressEnabled = if ($LocalAccess) { "true" } else { "false" }

helm upgrade --install colpali-stack "./modules/helm/colpali-stack" `
    --namespace colpali-stack --create-namespace `
    --set acrServer="$($env['ACR_LOGIN_SERVER'])" `
    --set aksIdentityClientId="$($env['WORKLOAD_IDENTITY_CLIENT_ID'])" `
    --set aksKubeletIdentityClientId="$($env['AKS_KUBELET_IDENTITY_CLIENT_ID'])" `
    --set tenantId="$($env['TENANT_ID'])" `
    --set storageAccountName="$($env['DATA_STORAGE_ACCOUNT_NAME'])" `
    --set serviceBusNamespace="$($env['SERVICE_BUS_NAMESPACE_NAME'])" `
    --set serviceBusQueueName="$($env['SERVICE_BUS_QUEUE_NAME'])" `
    --set documentProcessor.imageTag="$(if ($env['DOCUMENT_PROCESSOR_IMAGE_TAG']) { $env['DOCUMENT_PROCESSOR_IMAGE_TAG'] } else { 'latest' })" `
    --set colpaliInference.imageTag="$(if ($env['COLPALI_INFERENCE_IMAGE_TAG']) { $env['COLPALI_INFERENCE_IMAGE_TAG'] } else { 'latest' })" `
    --set colpaliInference.vllm.imageTag="$(if ($env['COLPALI_INFERENCE_VLLM_IMAGE_TAG']) { $env['COLPALI_INFERENCE_VLLM_IMAGE_TAG'] } else { 'latest' })" `
    --set agentApi.enabled=true --set agentApi.imageTag="$(if ($env['AGENT_API_IMAGE_TAG']) { $env['AGENT_API_IMAGE_TAG'] } else { 'latest' })" `
    --set aiFoundryOpenAiEndpoint="$($env['AI_FOUNDRY_OPEN_AI_ENDPOINT'])" `
    --set modelName="$($env['MODEL_NAME'])" `
    --set agentUi.enabled=true --set agentUi.imageTag="$(if ($env['AGENT_UI_IMAGE_TAG']) { $env['AGENT_UI_IMAGE_TAG'] } else { 'latest' })" `
    --set keyVault.name="$($env['KEY_VAULT_NAME'])" `
    --set keyVault.qdrantApiKeySecretName="$($env['QDRANT_API_KEY_SECRET_NAME'])" `
    --set keyVault.qdrantReadOnlyApiKeySecretName="$($env['QDRANT_READ_ONLY_API_KEY_SECRET_NAME'])" `
    --set keyVault.applicationInsightsConnectionStringSecretName="$($env['APPLICATION_INSIGHTS_CONNECTION_STRING_SECRET_NAME'])" `
    --set ingress-nginx-agent-ui.enabled=true `
    --set ingress-nginx-colpali.enabled=$ingressEnabled `
    --set ingress-nginx-qdrant.enabled=$ingressEnabled `
    --wait --timeout 30m

if ($LASTEXITCODE -ne 0) { throw "Helm deployment failed" }

Write-Host "Deployment complete. Checking status..."
kubectl get pods -n colpali-stack

# Get and display ingress IPs
$maxWait = 120; $elapsed = 0
$agentUiIp = ""
while ($elapsed -lt $maxWait -and -not $agentUiIp) {
    $agentUiIp = kubectl get svc colpali-stack-ingress-nginx-agent-ui-controller -n colpali-stack -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
    if (-not $agentUiIp) { Start-Sleep 10; $elapsed += 10 }
}

if ($agentUiIp) {
    Write-Host "Agent UI: http://$agentUiIp"
    # Update .env
    $content = Get-Content $EnvFile | Where-Object { $_ -notmatch '^AGENT_UI_ENDPOINT=' }
    $content += "AGENT_UI_ENDPOINT=http://$agentUiIp"
    Set-Content $EnvFile $content
}

if ($LocalAccess) {
    $colpaliIp = kubectl get svc colpali-stack-ingress-nginx-colpali-controller -n colpali-stack -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
    $qdrantIp = kubectl get svc colpali-stack-ingress-nginx-qdrant-controller -n colpali-stack -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null
    if ($colpaliIp) { Write-Host "ColPali: http://$colpaliIp" }
    if ($qdrantIp) { Write-Host "Qdrant: http://$qdrantIp" }
}

Write-Host "Done!"
