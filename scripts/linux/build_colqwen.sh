#!/bin/bash#!/bin/bash



# Build and push ColQwen inference and downloader containers# Build and push ColQwen inference and downloader containers



set -eset -e



echo "Building ColQwen Containers"echo "Building ColQwen Containers"



# Load .env file# Load .env file

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"

ENV_FILE="$PROJECT_ROOT/.env"ENV_FILE="$PROJECT_ROOT/.env"



if [ ! -f "$ENV_FILE" ]; thenif [ ! -f "$ENV_FILE" ]; then

    echo ".env file not found at $ENV_FILE"    echo ".env file not found at $ENV_FILE"

    echo "Run deploy_infra.sh first."    echo "Run deploy_infra.sh first."

    exit 1    exit 1

fifi



# Parse .env file# Parse .env file

while IFS='=' read -r key value; dowhile IFS='=' read -r key value; do

    # Skip comments and empty lines    # Skip comments and empty lines

    if [[ $key =~ ^[[:space:]]*# ]] || [[ -z $key ]]; then    if [[ $key =~ ^[[:space:]]*# ]] || [[ -z $key ]]; then

        continue        continue

    fi    fi

    # Remove any trailing whitespace and export    # Remove any trailing whitespace and export

    key=$(echo "$key" | xargs)    key=$(echo "$key" | xargs)

    value=$(echo "$value" | xargs)    value=$(echo "$value" | xargs)

    export "$key"="$value"    export "$key"="$value"

done < "$ENV_FILE"done < "$ENV_FILE"



# Get ACR name from .env# Get ACR name from .env

if [ -z "$ACR_NAME" ]; thenif [ -z "$ACR_NAME" ]; then

    echo "ACR_NAME not found in .env file"    echo "ACR_NAME not found in .env file"

    exit 1    exit 1

fifi



# Generate unique tag using git commit hash# Generate unique tag using git commit hash

if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; thenif command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then

    IMAGE_TAG=$(git rev-parse --short HEAD)    IMAGE_TAG=$(git rev-parse --short HEAD)

    echo "Generated image tag from git hash: $IMAGE_TAG"    echo "Generated image tag from git hash: $IMAGE_TAG"

elseelse

    echo "Git not available, using timestamp as fallback"    echo "Git not available, using timestamp as fallback"

    IMAGE_TAG=$(date +"%Y%m%d-%H%M%S")    IMAGE_TAG=$(date +"%Y%m%d-%H%M%S")

fifi



# Navigate to colpali directory# Navigate to colpali directory

COLPALI_DIR="$PROJECT_ROOT/modules/colpali"COLPALI_DIR="$PROJECT_ROOT/modules/colpali"

cd "$COLPALI_DIR"cd "$COLPALI_DIR"



# Login to ACR# Login to ACR

echo "Logging into Azure Container Registry: $ACR_NAME"echo "Logging into Azure Container Registry: $ACR_NAME"

az acr login --name "$ACR_NAME"az acr login --name "$ACR_NAME"

if [ $? -ne 0 ]; thenif [ $? -ne 0 ]; then

    echo "Failed to login to ACR"    echo "Failed to login to ACR"

    exit 1    exit 1

fifi



# Build and push image remotely with cache# Build and push image remotely with cache

echo "Building ColQwen unified image remotely with cache..."echo "Building ColQwen unified image remotely with cache..."

INFERENCE_IMAGE="$ACR_NAME.azurecr.io/colqwen-inference:$IMAGE_TAG"INFERENCE_IMAGE="$ACR_NAME.azurecr.io/colqwen-inference:$IMAGE_TAG"



az acr build --registry "$ACR_NAME" --image "$INFERENCE_IMAGE" --file Dockerfile .az acr build --registry "$ACR_NAME" --image "$INFERENCE_IMAGE" --file Dockerfile --no-logs .



echo "ColQwen image built and pushed successfully!"echo "ColQwen image built and pushed successfully!"

echo "   Image: $INFERENCE_IMAGE"echo "   Image: $INFERENCE_IMAGE"

echo "   Tag: $IMAGE_TAG"echo "   Tag: $IMAGE_TAG"



# Update .env file with the image tag# Update .env file with the image tag

TEMP_FILE=$(mktemp)TEMP_FILE=$(mktemp)

COLQWEN_TAG_UPDATED=falseCOLQWEN_TAG_UPDATED=false



while IFS= read -r line; dowhile IFS= read -r line; do

    if [[ $line =~ ^COLQWEN_IMAGE_TAG= ]]; then    if [[ $line =~ ^COLQWEN_IMAGE_TAG= ]]; then

        echo "COLQWEN_IMAGE_TAG=$IMAGE_TAG" >> "$TEMP_FILE"        echo "COLQWEN_IMAGE_TAG=$IMAGE_TAG" >> "$TEMP_FILE"

        COLQWEN_TAG_UPDATED=true        COLQWEN_TAG_UPDATED=true

    else    else

        echo "$line" >> "$TEMP_FILE"        echo "$line" >> "$TEMP_FILE"

    fi    fi

done < "$ENV_FILE"done < "$ENV_FILE"



# Add COLQWEN_IMAGE_TAG if it doesn't exist# Add COLQWEN_IMAGE_TAG if it doesn't exist

if [ "$COLQWEN_TAG_UPDATED" = false ]; thenif [ "$COLQWEN_TAG_UPDATED" = false ]; then

    echo "COLQWEN_IMAGE_TAG=$IMAGE_TAG" >> "$TEMP_FILE"    echo "COLQWEN_IMAGE_TAG=$IMAGE_TAG" >> "$TEMP_FILE"

fifi



mv "$TEMP_FILE" "$ENV_FILE"mv "$TEMP_FILE" "$ENV_FILE"

echo "Updated .env with COLQWEN_IMAGE_TAG=$IMAGE_TAG"echo "Updated .env with COLQWEN_IMAGE_TAG=$IMAGE_TAG"



echo "ColQwen container build completed successfully!"echo "ColQwen container build completed successfully!"

echo "Run 'apply_helm.sh' to deploy the updated image to AKS"echo "Run 'apply_helm.sh' to deploy the updated image to AKS"
