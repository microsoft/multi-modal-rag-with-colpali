#!/bin/bash

# Build and push document processor container using local Docker for better caching

set -e

echo "Building Document Processor Container (Local Docker Build)"

# Load .env file
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
ENV_FILE="$PROJECT_ROOT/.env"

if [ ! -f "$ENV_FILE" ]; then
    echo ".env file not found at $ENV_FILE"
    echo "Run deploy_infra.sh first."
    exit 1
fi

# Parse .env file
while IFS='=' read -r key value; do
    # Skip comments and empty lines
    if [[ $key =~ ^[[:space:]]*# ]] || [[ -z $key ]]; then
        continue
    fi
    # Remove any trailing whitespace and export
    key=$(echo "$key" | xargs)
    value=$(echo "$value" | xargs)
    export "$key"="$value"
done < "$ENV_FILE"

# Get ACR name from .env
if [ -z "$ACR_NAME" ]; then
    echo "ACR_NAME not found in .env file"
    exit 1
fi

# Generate unique tag using git commit hash
if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    IMAGE_TAG=$(git rev-parse --short HEAD)
    echo "Generated image tag from git hash: $IMAGE_TAG"
else
    echo "Git not available, using timestamp as fallback"
    IMAGE_TAG=$(date +"%Y%m%d-%H%M%S")
fi

# Navigate to document processor directory
DOC_PROCESSOR_DIR="$PROJECT_ROOT/modules/document_processor"
cd "$DOC_PROCESSOR_DIR"

# Login to ACR
echo "Logging into Azure Container Registry: $ACR_NAME"
az acr login --name "$ACR_NAME"
if [ $? -ne 0 ]; then
    echo "Failed to login to ACR"
    exit 1
fi

# Build locally with Docker (leverages local cache)
FULL_IMAGE_NAME="$ACR_NAME.azurecr.io/document-processor:$IMAGE_TAG"

echo "Building Docker image locally: $FULL_IMAGE_NAME"
docker build --tag "$FULL_IMAGE_NAME" .
if [ $? -ne 0 ]; then
    echo "Docker build failed"
    exit 1
fi

# Push the image to ACR
echo "Pushing image to ACR: $FULL_IMAGE_NAME"
docker push "$FULL_IMAGE_NAME"
if [ $? -ne 0 ]; then
    echo "Failed to push image to ACR"
    exit 1
fi

echo "Container built and pushed successfully with local Docker caching!"

# Save the image tag to .env file for deployment
TMP_FILE=$(mktemp)
TAG_UPDATED=false

while IFS= read -r line; do
    if [[ $line =~ ^DOCUMENT_PROCESSOR_IMAGE_TAG= ]]; then
        echo "DOCUMENT_PROCESSOR_IMAGE_TAG=$IMAGE_TAG" >> "$TMP_FILE"
        TAG_UPDATED=true
    else
        echo "$line" >> "$TMP_FILE"
    fi
done < "$ENV_FILE"

# Add tag if not found
if [ "$TAG_UPDATED" = false ]; then
    echo "DOCUMENT_PROCESSOR_IMAGE_TAG=$IMAGE_TAG" >> "$TMP_FILE"
fi

mv "$TMP_FILE" "$ENV_FILE"

echo "Image tag $IMAGE_TAG saved to .env file"

if [ $? -eq 0 ]; then
    echo "Container built and pushed successfully"

    # Check if Container App exists and update revision
    if [ -n "$DOCUMENT_PROCESSOR_CONTAINER_APP_NAME" ] && [ -n "$RESOURCE_GROUP" ]; then
        echo "Checking if Container App '$DOCUMENT_PROCESSOR_CONTAINER_APP_NAME' exists..."
        CONTAINER_APP_EXISTS=$(az containerapp show --name "$DOCUMENT_PROCESSOR_CONTAINER_APP_NAME" --resource-group "$RESOURCE_GROUP" --query "name" -o tsv 2>/dev/null || true)

        if [ -n "$CONTAINER_APP_EXISTS" ]; then
            echo "Updating Container App revision with new image tag: $IMAGE_TAG"

            # Update the container app with the new image
            REVISION_SUFFIX=$(echo "$IMAGE_TAG" | tr '.' '-' | tr '_' '-')

            az containerapp update \
                --name "$DOCUMENT_PROCESSOR_CONTAINER_APP_NAME" \
                --resource-group "$RESOURCE_GROUP" \
                --image "$FULL_IMAGE_NAME" \
                --revision-suffix "$REVISION_SUFFIX"

            if [ $? -eq 0 ]; then
                echo "Container App revision updated successfully"
            else
                echo "Warning: Failed to update Container App revision. You may need to redeploy manually."
            fi
        else
            echo "Container App '$DOCUMENT_PROCESSOR_CONTAINER_APP_NAME' not found. Deploy infrastructure first with container apps enabled."
        fi
    else
        echo "Container App name or resource group not found in .env file. Skipping revision update."
    fi
else
    echo "Build failed"
    exit 1
fi
