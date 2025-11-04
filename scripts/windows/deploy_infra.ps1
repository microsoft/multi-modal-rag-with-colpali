#!/usr/bin/env pwsh
# Complete deployment script: deploys Bicep infrastructure
# Usage: .\deploy_infra.ps1 [-ResourceGroup <resource-group>] [-DeployRoles <true|false>] [-DeployContainerApps <true|false>] [-DeployEventGrid <true|false>]
# Note: baseName and location are defined in infra/src/main.bicepparam

[CmdletBinding()]
param(
    [Parameter()]
    [string]$ResourceGroup = "colqwen-rg",

    [Parameter()]
    [string]$DeployRoles = "true",

    [Parameter()]
    [string]$DeployContainerApps = "false",

    [Parameter()]
    [string]$DeployEventGrid = "false"
)

$ErrorActionPreference = "Stop"

# Get the absolute path of the script directory
$ScriptDir = $PSScriptRoot
# Get the absolute path of the project root (go up two levels: scripts/windows -> scripts -> root)
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptDir)
# Change to the project root directory
Set-Location $ProjectRoot

Write-Host "Project root: $ProjectRoot"

# Validate mutually exclusive parameters
if ($DeployContainerApps -eq "true" -and $DeployEventGrid -eq "true") {
    throw "DeployContainerApps and DeployEventGrid cannot both be true - they must be mutually exclusive. Deploy Container Apps first, then Event Grid separately."
}

# Get document processor image tag from .env file if deploying container apps
$DocumentProcessorImageTag = "latest"
if ($DeployContainerApps -eq "true") {
    $EnvFile = Join-Path $ProjectRoot ".env"
    if (Test-Path $EnvFile) {
        Write-Host "Reading document processor image tag from .env file..."
        $envContent = Get-Content $EnvFile
        foreach ($line in $envContent) {
            if ($line -match '^DOCUMENT_PROCESSOR_IMAGE_TAG=(.*)$') {
                $DocumentProcessorImageTag = $matches[1]
                Write-Host "Using document processor image tag: $DocumentProcessorImageTag"
                break
            }
        }
    }
    else {
        Write-Host "No .env file found, using default image tag: $DocumentProcessorImageTag"
    }
}

# Get user object ID
Write-Host "Getting user object ID..."
$UserObjectId = az ad signed-in-user show --query id -o tsv
if ($LASTEXITCODE -ne 0) {
    throw "Failed to get user object ID"
}

# Check if online endpoint already exists
Write-Host "Checking if online endpoint already exists..."
$BicepParamContent = Get-Content "$ProjectRoot\infra\src\main.bicepparam" -Raw
$BaseName = if ($BicepParamContent -match "param baseName = '([^']+)'") { $Matches[1] } else { throw "Could not find baseName in main.bicepparam" }
$EndpointName = "oep-$BaseName"
$WorkspaceNameFromParam = "mlw-$BaseName"

$EndpointExists = $false
try {
    # Use az resource show instead of az ml for better performance
    $ResourceId = "/subscriptions/$((az account show --query id -o tsv))/resourceGroups/$ResourceGroup/providers/Microsoft.MachineLearningServices/workspaces/$WorkspaceNameFromParam/onlineEndpoints/$EndpointName"
    $EndpointCheck = az resource show --ids "$ResourceId" 2>$null
    if ($LASTEXITCODE -eq 0 -and $EndpointCheck) {
        $EndpointExists = $true
        Write-Host "  Endpoint '$EndpointName' already exists - will skip creation to preserve traffic allocation"
    }
}
catch {
    # Endpoint doesn't exist, which is fine for first deployment
}

if (-not $EndpointExists) {
    Write-Host "  Endpoint '$EndpointName' does not exist - will create it"
}

$CreateEndpoint = if ($EndpointExists) { "false" } else { "true" }

# Use absolute paths for the Bicep files
$BicepParamFile = Join-Path $ProjectRoot "infra\src\main.bicepparam"
Write-Host "Using Bicep parameter file: $BicepParamFile"

# Deploy resources with Bicep using the parameter file
Write-Host "Deploying resources with Bicep..."
Write-Host "Resource Group: '$ResourceGroup'"
Write-Host "Bicep Param File: '$BicepParamFile'"
Write-Host "User Object ID: '$UserObjectId'"
Write-Host "Deploy Roles: '$DeployRoles'"
Write-Host "Deploy Container Apps: '$DeployContainerApps'"
Write-Host "Deploy Event Grid: '$DeployEventGrid'"

$deploymentOutput = az deployment group create --resource-group "$ResourceGroup" --parameters "$BicepParamFile" userObjectId="$UserObjectId" deployRoleAssignments="$DeployRoles" deployContainerApps="$DeployContainerApps" createOnlineEndpoint="$CreateEndpoint" documentProcessorImageTag="$DocumentProcessorImageTag" --query "properties.outputs" -o json | ConvertFrom-Json

if ($LASTEXITCODE -ne 0) {
    throw "Bicep deployment failed"
}

# Get values from Bicep outputs
$WorkspaceName = $deploymentOutput.amlWorkspaceName.value
$ComputeClusterName = $deploymentOutput.amlComputeClusterName.value
$EmbeddingEndpointName = $deploymentOutput.amlEmbeddingEndpointName.value
$EmbeddingEndpointUrl = $deploymentOutput.amlEmbeddingEndpointUrl.value
$AcrName = $deploymentOutput.acrName.value
$AcrLoginServer = $deploymentOutput.acrLoginServer.value
$EmbeddingEndpointType = $deploymentOutput.amlEmbeddingEndpointType.value
$EmbeddingEndpointCount = $deploymentOutput.amlEmbeddingEndpointCount.value

# Container Apps outputs (only available when DeployContainerApps = true)
if ($DeployContainerApps -eq "true") {
    $QdrantEndpoint = $deploymentOutput.qdrantEndpoint.value
    $DocProcessorEndpoint = $deploymentOutput.docProcessorEndpoint.value
    $ContainerAppsEnvironmentName = $deploymentOutput.containerAppsEnvironmentName.value
    $DocProcessorContainerAppName = $deploymentOutput.docProcessorContainerAppName.value

}
else {
    $QdrantEndpoint = ""
    $DocProcessorEndpoint = ""
    $ContainerAppsEnvironmentName = ""
    $DocProcessorContainerAppName = ""
    $LoggingInfo = ""
}

Write-Host "Deployment outputs:"
Write-Host "  AML Workspace: $WorkspaceName"
Write-Host "  AML Compute Cluster: $ComputeClusterName"
Write-Host "  Embedding Endpoint Name: $EmbeddingEndpointName"
Write-Host "  Embedding Endpoint URL: $EmbeddingEndpointUrl"
Write-Host "  ACR Name: $AcrName"
Write-Host "  ACR Login Server: $AcrLoginServer"
Write-Host "  Embedding Endpoint Type: $EmbeddingEndpointType"
Write-Host "  Embedding Endpoint Count: $EmbeddingEndpointCount"

if ($DeployContainerApps -eq "true") {
    Write-Host "  QDRANT Endpoint: $QdrantEndpoint"
    Write-Host "  Doc Processor Endpoint: $DocProcessorEndpoint"
    Write-Host "  Container Apps Environment: $ContainerAppsEnvironmentName"
    Write-Host "  Logging: Integrated with AML Application Insights workspace"
}
else {
    Write-Host "  Container Apps: Not deployed (use -DeployContainerApps true to deploy)"
}

if ($DeployEventGrid -eq "true") {
    Write-Host "  Event Grid: Deployed and configured for blob events"
}
else {
    Write-Host "  Event Grid: Not deployed (use -DeployEventGrid true to deploy)"
}

# Get subscription ID
Write-Host "Getting subscription ID..."
$SubscriptionId = az account show --query id -o tsv
if ($LASTEXITCODE -ne 0) {
    throw "Failed to get subscription ID"
}

# Get ACR credentials
Write-Host "Retrieving ACR credentials..."
$AcrCredentials = az acr credential show --name $AcrName --query "{username:username, password:passwords[0].value}" -o json | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {
    Write-Host "Warning: Failed to retrieve ACR credentials. They will not be added to .env file."
    $AcrUsername = ""
    $AcrPassword = ""
}
else {
    $AcrUsername = $AcrCredentials.username
    $AcrPassword = $AcrCredentials.password
}

# Preserve existing DOCUMENT_PROCESSOR_IMAGE_TAG if it exists
$ExistingImageTag = ""
$EnvFile = Join-Path $ProjectRoot ".env"
if (Test-Path $EnvFile) {
    $existingContent = Get-Content $EnvFile
    foreach ($line in $existingContent) {
        if ($line -match '^DOCUMENT_PROCESSOR_IMAGE_TAG=(.*)$') {
            $ExistingImageTag = $matches[1]
            Write-Host "Preserving existing image tag: $ExistingImageTag"
            break
        }
    }
}

# Create .env file in project root
Write-Host "Creating .env file at $EnvFile"

$EnvContent = @"
RESOURCE_GROUP=$ResourceGroup
SUBSCRIPTION_ID=$SubscriptionId
AML_WORKSPACE_NAME=$WorkspaceName
AML_COMPUTE_NAME=$ComputeClusterName
AML_EMBEDDING_ENDPOINT_NAME=$EmbeddingEndpointName
AML_EMBEDDING_ENDPOINT_URL=$EmbeddingEndpointUrl
AML_EMBEDDING_ENDPOINT_TYPE=$EmbeddingEndpointType
AML_EMBEDDING_ENDPOINT_COUNT=$EmbeddingEndpointCount
ACR_NAME=$AcrName
ACR_LOGIN_SERVER=$AcrLoginServer
ACR_USERNAME=$AcrUsername
ACR_PASSWORD=$AcrPassword
QDRANT_ENDPOINT=$QdrantEndpoint
QDRANT_COLLECTION_NAME=colpali-documents
DOC_PROCESSOR_ENDPOINT=$DocProcessorEndpoint
DOCUMENT_PROCESSOR_CONTAINER_APP_NAME=$DocProcessorContainerAppName
CONTAINER_APPS_ENVIRONMENT=$ContainerAppsEnvironmentName
"@

# Add the image tag if it exists
if ($ExistingImageTag) {
    $EnvContent += "`nDOCUMENT_PROCESSOR_IMAGE_TAG=$ExistingImageTag"
}

Set-Content -Path $EnvFile -Value $EnvContent -NoNewline

Write-Host "Deployment and device creation complete."
Write-Host ".env file created with deployment outputs."
