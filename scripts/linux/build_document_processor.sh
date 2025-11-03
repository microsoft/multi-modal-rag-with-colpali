#!/bin/bash

# Build and push document processor container

set -e

echo "Building Document Processor Container"

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

# Navigate to document processor directory
DOC_PROCESSOR_DIR="$PROJECT_ROOT/modules/document_processor"

cd "$DOC_PROCESSOR_DIR"

# Build and push container image
echo "Building and pushing to $ACR_NAME..."

az acr build --registry "$ACR_NAME" --image "document-processor:latest" .

if [ $? -eq 0 ]; then
    echo "Container built and pushed successfully"
else
    echo "Build failed"
    exit 1
fi
