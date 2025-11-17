#!/bin/bash
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

# Universal container build script
# Parameters:
#   $1: SERVICE_NAME - Display name for the service being built
#   $2: IMAGE_NAME - Name for the Docker image (e.g., "colqwen-inference")
#   $3: DIRECTORY - Relative path to the module directory (e.g., "modules/colqwen_inference")
#   $4: DOCKERFILE - Name of the Dockerfile (default: "Dockerfile")
#   $5: ENV_VAR_NAME - Name of the environment variable to update (e.g., "COLQWEN_INFERENCE_IMAGE_TAG")

set -e

# Check required parameters
if [ $# -lt 4 ]; then
    echo "Usage: $0 <SERVICE_NAME> <IMAGE_NAME> <DIRECTORY> <ENV_VAR_NAME> [DOCKERFILE]"
    echo "Example: $0 'ColQwen Inference' 'colqwen-inference' 'modules/colqwen_inference' 'COLQWEN_INFERENCE_IMAGE_TAG' 'Dockerfile'"
    exit 1
fi

SERVICE_NAME="$1"
IMAGE_NAME="$2"
DIRECTORY="$3"
ENV_VAR_NAME="$4"
DOCKERFILE="${5:-Dockerfile}"

echo "Building $SERVICE_NAME Container"

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

# Navigate to the specified directory
TARGET_DIR="$PROJECT_ROOT/$DIRECTORY"
cd "$TARGET_DIR"

# Login to ACR
echo "Logging into Azure Container Registry: $ACR_NAME"
az acr login --name "$ACR_NAME"
if [ $? -ne 0 ]; then
    echo "Failed to login to ACR"
    exit 1
fi

# Build and push image remotely with cache
echo "Building $SERVICE_NAME image remotely with cache..."
FULL_IMAGE_NAME="$ACR_NAME.azurecr.io/$IMAGE_NAME:$IMAGE_TAG"

az acr build --registry "$ACR_NAME" --image "$FULL_IMAGE_NAME" --file "$DOCKERFILE" .

if [ $? -ne 0 ]; then
    echo "Failed to build $SERVICE_NAME image"
    exit 1
fi

echo "$SERVICE_NAME image built and pushed successfully!"
echo "   Image: $FULL_IMAGE_NAME"
echo "   Tag: $IMAGE_TAG"

# Update .env file with the image tag
TEMP_FILE=$(mktemp)
TAG_UPDATED=false

while IFS= read -r line; do
    if [[ $line =~ ^$ENV_VAR_NAME= ]]; then
        echo "$ENV_VAR_NAME=$IMAGE_TAG" >> "$TEMP_FILE"
        TAG_UPDATED=true
    else
        echo "$line" >> "$TEMP_FILE"
    fi
done < "$ENV_FILE"

# Add environment variable if it doesn't exist
if [ "$TAG_UPDATED" = false ]; then
    echo "$ENV_VAR_NAME=$IMAGE_TAG" >> "$TEMP_FILE"
fi

mv "$TEMP_FILE" "$ENV_FILE"
echo "Updated .env with $ENV_VAR_NAME=$IMAGE_TAG"

echo "$SERVICE_NAME container build completed successfully!"
echo "Run 'apply_helm.sh' to deploy the updated image to AKS"
