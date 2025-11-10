#!/bin/bash
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# Build and push ColQwen inference and downloader containers

set -e

echo "Building ColQwen Containers"

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

# Navigate to colpali directory
COLPALI_DIR="$PROJECT_ROOT/modules/colpali"
cd "$COLPALI_DIR"

# Login to ACR
echo "Logging into Azure Container Registry: $ACR_NAME"
az acr login --name "$ACR_NAME"
if [ $? -ne 0 ]; then
    echo "Failed to login to ACR"
    exit 1
fi

# Build and push image remotely with cache
echo "Building ColQwen unified image remotely with cache..."
INFERENCE_IMAGE="$ACR_NAME.azurecr.io/colqwen-inference:$IMAGE_TAG"

az acr build --registry "$ACR_NAME" --image "$INFERENCE_IMAGE" --file Dockerfile .

echo "ColQwen image built and pushed successfully!"
echo "   Image: $INFERENCE_IMAGE"
echo "   Tag: $IMAGE_TAG"

# Update .env file with the image tag
TEMP_FILE=$(mktemp)
COLQWEN_TAG_UPDATED=false

while IFS= read -r line; do
    if [[ $line =~ ^COLQWEN_IMAGE_TAG= ]]; then
        echo "COLQWEN_IMAGE_TAG=$IMAGE_TAG" >> "$TEMP_FILE"
        COLQWEN_TAG_UPDATED=true
    else
        echo "$line" >> "$TEMP_FILE"
    fi
done < "$ENV_FILE"

# Add COLQWEN_IMAGE_TAG if it doesn't exist
if [ "$COLQWEN_TAG_UPDATED" = false ]; then
    echo "COLQWEN_IMAGE_TAG=$IMAGE_TAG" >> "$TEMP_FILE"
fi

mv "$TEMP_FILE" "$ENV_FILE"
echo "Updated .env with COLQWEN_IMAGE_TAG=$IMAGE_TAG"

echo "ColQwen container build completed successfully!"
echo "Run 'apply_helm.sh' to deploy the updated image to AKS"
