#!/usr/bin/env pwsh

# Build and push document processor container using local Docker for better caching

$ErrorActionPreference = "Stop"

Write-Host "Building Document Processor Container (Local Docker Build)"

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

# Generate unique tag using git commit hash
try {
    $imageTag = (git rev-parse --short HEAD 2>$null)
    Write-Host "Generated image tag from git hash: $imageTag"
}
catch {
    Write-Host "Git not available, using timestamp as fallback"
    $imageTag = (Get-Date -Format "yyyyMMdd-HHmmss")
}

# Navigate to document processor directory
$docProcessorDir = Join-Path $PSScriptRoot "..\..\modules\document_processor"
Push-Location $docProcessorDir

try {
    # Login to ACR
    Write-Host "Logging into Azure Container Registry: $acrName"
    az acr login --name $acrName
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to login to ACR"
    }

    # Build locally with Docker (leverages local cache)
    $fullImageName = "${acrName}.azurecr.io/document-processor:${imageTag}"

    Write-Host "Building Docker image locally: $fullImageName"
    docker build --tag $fullImageName .
    if ($LASTEXITCODE -ne 0) {
        throw "Docker build failed"
    }

    # Push the image to ACR
    Write-Host "Pushing image to ACR: $fullImageName"
    docker push $fullImageName
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to push image to ACR"
    }

    Write-Host "Container built and pushed successfully with local Docker caching!"

    # Save the image tag to .env file for deployment
    $envContent = Get-Content $EnvFile
    $newContent = @()
    $tagUpdated = $false

    foreach ($line in $envContent) {
        if ($line -match '^DOCUMENT_PROCESSOR_IMAGE_TAG=') {
            $newContent += "DOCUMENT_PROCESSOR_IMAGE_TAG=$imageTag"
            $tagUpdated = $true
        }
        else {
            $newContent += $line
        }
    }

    # Add tag if not found
    if (-not $tagUpdated) {
        $newContent += "DOCUMENT_PROCESSOR_IMAGE_TAG=$imageTag"
    }

    $newContent | Set-Content $EnvFile
    Write-Host "Image tag $imageTag saved to .env file"

    # Check if Container App exists and update revision
    $containerAppName = $envVars['DOCUMENT_PROCESSOR_CONTAINER_APP_NAME']
    $resourceGroup = $envVars['RESOURCE_GROUP']

    if ($containerAppName -and $resourceGroup) {
        Write-Host "Checking if Container App '$containerAppName' exists..."
        $containerAppExists = az containerapp show --name $containerAppName --resource-group $resourceGroup --query "name" -o tsv 2>$null

        if ($containerAppExists) {
            Write-Host "Updating Container App revision with new image tag: $imageTag"

            # Update the container app with the new image
            az containerapp update `
                --name $containerAppName `
                --resource-group $resourceGroup `
                --image $fullImageName `
                --revision-suffix $imageTag.Replace('.', '-').Replace('_', '-')

            if ($LASTEXITCODE -eq 0) {
                Write-Host "Container App revision updated successfully"
            }
            else {
                Write-Host "Warning: Failed to update Container App revision. You may need to redeploy manually."
            }
        }
        else {
            Write-Host "Container App '$containerAppName' not found. Deploy infrastructure first with container apps enabled."
        }
    }
    else {
        Write-Host "Container App name or resource group not found in .env file. Skipping revision update."
    }

}
finally {
    Pop-Location
}
