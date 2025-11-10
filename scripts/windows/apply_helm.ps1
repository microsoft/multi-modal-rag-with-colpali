
#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ColPali Pure Kubernetes Deployment Script
#
# Deploys the complete ColPali stack to AKS using pure Kubernetes approach:
# - ColQwen model download Job with shared storage
# - ColQwen inference Deployment with auto-scaling
# - Document processor service
# - Qdrant vector database
# - Ingress controller for external access
#
# Prerequisites: Infrastructure must be deployed first via deploy_infra.ps1
# Reads configuration from .env file created by infrastructure deployment.

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

# Setup paths
$ScriptDir = $PSScriptRoot
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
Set-Location $ProjectRoot

Write-Host "ColPali Kubernetes Deployment Starting" -ForegroundColor White

# Load configuration from .env file
$EnvFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path $EnvFile)) {
    Write-Error ".env file not found. Infrastructure deployment required first."
    Write-Error "Run deploy_infra.ps1 to create infrastructure and configuration."
    exit 1
}

Write-Host "Loading configuration from .env file"
$envVars = @{}
Get-Content $EnvFile | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') {
        $envVars[$matches[1]] = $matches[2]
    }
}

# Validate required configuration
$requiredVars = @(
    'RESOURCE_GROUP',
    'AKS_CLUSTER_NAME',
    'ACR_LOGIN_SERVER',
    'WORKLOAD_IDENTITY_CLIENT_ID',
    'AKS_KUBELET_IDENTITY_CLIENT_ID',
    'DATA_STORAGE_ACCOUNT_NAME',
    'SERVICE_BUS_NAMESPACE_NAME',
    'SERVICE_BUS_QUEUE_NAME',
    'KEY_VAULT_NAME',
    'QDRANT_API_KEY_SECRET_NAME',
    'QDRANT_READ_ONLY_API_KEY_SECRET_NAME',
    'APPLICATION_INSIGHTS_CONNECTION_STRING_SECRET_NAME'
)

foreach ($var in $requiredVars) {
    if (-not $envVars.ContainsKey($var) -or [string]::IsNullOrEmpty($envVars[$var])) {
        Write-Error "Missing required configuration: $var"
        Write-Error "Ensure infrastructure deployment completed successfully."
        exit 1
    }
}

Write-Host "Configuration loaded successfully"
Write-Host "Resource Group: $($envVars['RESOURCE_GROUP'])"
Write-Host "AKS Cluster: $($envVars['AKS_CLUSTER_NAME'])"

# Connect to AKS cluster
Write-Host "Connecting to AKS cluster"
az aks get-credentials --resource-group $envVars['RESOURCE_GROUP'] --name $envVars['AKS_CLUSTER_NAME'] --overwrite-existing

if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to get AKS credentials"
    exit 1
}

# Verify cluster connectivity
Write-Host "Verifying cluster connectivity"
kubectl cluster-info --request-timeout=10s | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to connect to AKS cluster"
    Write-Error "Check your credentials and network connection"
    exit 1
}

Write-Host "Successfully connected to AKS cluster"

# Update Helm dependencies
Write-Host "Updating Helm chart dependencies"
Push-Location "helm/colpali-stack"
try {
    helm dependency update
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to update Helm dependencies"
        exit 1
    }
    Write-Host "Helm dependencies updated successfully"
}
finally {
    Pop-Location
}

# Deploy ColPali stack
Write-Host "Deploying ColPali stack to Kubernetes"
Write-Host "This may take several minutes..."

# Get image tags from .env
$docProcessorImageTag = if ($envVars.ContainsKey('DOCUMENT_PROCESSOR_IMAGE_TAG')) { $envVars['DOCUMENT_PROCESSOR_IMAGE_TAG'] } else { "latest" }
$colqwenImageTag = if ($envVars.ContainsKey('COLQWEN_IMAGE_TAG')) { $envVars['COLQWEN_IMAGE_TAG'] } else { "latest" }

# Deploy ColPali stack with pure Kubernetes approach
helm upgrade --install colpali-stack "./helm/colpali-stack" `
    --namespace colpali-stack `
    --create-namespace `
    --set acrServer="$($envVars['ACR_LOGIN_SERVER'])" `
    --set aksIdentityClientId="$($envVars['WORKLOAD_IDENTITY_CLIENT_ID'])" `
    --set aksKubeletIdentityClientId="$($envVars['AKS_KUBELET_IDENTITY_CLIENT_ID'])" `
    --set tenantId="$($envVars['TENANT_ID'])" `
    --set storageAccountName="$($envVars['DATA_STORAGE_ACCOUNT_NAME'])" `
    --set serviceBusNamespace="$($envVars['SERVICE_BUS_NAMESPACE_NAME'])" `
    --set serviceBusQueueName="$($envVars['SERVICE_BUS_QUEUE_NAME'])" `
    --set documentProcessor.imageTag="$docProcessorImageTag" `
    --set colqwenInference.imageTag="$colqwenImageTag" `
    --set keyVault.name="$($envVars['KEY_VAULT_NAME'])" `
    --set keyVault.qdrantApiKeySecretName="$($envVars['QDRANT_API_KEY_SECRET_NAME'])" `
    --set keyVault.qdrantReadOnlyApiKeySecretName="$($envVars['QDRANT_READ_ONLY_API_KEY_SECRET_NAME'])" `
    --set keyVault.applicationInsightsConnectionStringSecretName="$($envVars['APPLICATION_INSIGHTS_CONNECTION_STRING_SECRET_NAME'])" `
    --wait --timeout 20m

if ($LASTEXITCODE -eq 0) {
    Write-Host "Deployment completed successfully"

    # Wait for services to stabilize
    Start-Sleep 5

    # Check deployment status
    Write-Host "Checking deployment status"
    kubectl get pods
    kubectl get services
    kubectl get ingress

    # Get ingress controller service details
    $ingressIp = kubectl get svc colpali-stack-ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>$null

    Write-Host "ColPali Stack Deployment Summary"
    Write-Host "Cluster: $($envVars['AKS_CLUSTER_NAME'])"
    Write-Host "Resource Group: $($envVars['RESOURCE_GROUP'])"

    if ($ingressIp -and $ingressIp -ne "") {
        Write-Host "LoadBalancer IP: $ingressIp"
        Write-Host "Qdrant Dashboard: http://$ingressIp"
    }
    else {
        Write-Host "LoadBalancer IP: Pending assignment"
        Write-Host "Run 'kubectl get svc colpali-stack-ingress-nginx-controller' to check LoadBalancer status"
        Write-Host "Access will be available at http://<EXTERNAL-IP>/qdrant/ when ready"
    }
    Write-Host "Authentication: Azure Workload Identity"

    # Check ColPali stack component status
    Write-Host ""
    Write-Host "Checking ColPali stack deployment status..."

    # Check document processor pods
    $docProcessorStatus = kubectl get pods -l app=colpali-stack-document-processor --no-headers 2>$null
    if ($docProcessorStatus -and $docProcessorStatus -match "Running") {
        Write-Host "Document Processor: Running" -ForegroundColor Green
    }
    else {
        Write-Host "Document Processor: Starting..." -ForegroundColor Yellow
    }

    # Check ColQwen inference pods
    $colqwenStatus = kubectl get pods -l app=colpali-stack-colqwen-inference --no-headers 2>$null
    if ($colqwenStatus -and $colqwenStatus -match "Running") {
        Write-Host "ColQwen Inference: Running" -ForegroundColor Green
    }
    else {
        Write-Host "ColQwen Inference: Starting..." -ForegroundColor Yellow
    }

    # Check Qdrant pods
    $qdrantStatus = kubectl get pods -l app.kubernetes.io/name=qdrant --no-headers 2>$null
    if ($qdrantStatus -and $qdrantStatus -match "Running") {
        Write-Host "Qdrant Vector Database: Running" -ForegroundColor Green
    }
    else {
        Write-Host "Qdrant Vector Database: Starting..." -ForegroundColor Yellow
    }

    # Check model download job status
    $modelJobStatus = kubectl get jobs -l app=colpali-stack-colqwen-model-download --no-headers 2>$null
    if ($modelJobStatus -and $modelJobStatus -match "1/1") {
        Write-Host "Model Download Job: Completed" -ForegroundColor Green
    }
    else {
        Write-Host "Model Download Job: In progress..." -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "Pure Kubernetes ColPali deployment completed!" -ForegroundColor Green

}
else {
    Write-Error "Deployment failed"
    Write-Error "Check the error messages above for details"
    exit 1
}
