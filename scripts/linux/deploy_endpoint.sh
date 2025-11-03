#!/bin/bash
# ColPali Endpoint Deployment Script
# Deploys registered ColPali models to Azure ML Online Endpoint
# Usage: ./deploy_endpoint.sh

set -e  # Exit on any error

# Get the absolute path of the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Get the absolute path of the project root (go up two levels: scripts/linux -> scripts -> root)
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
COLPALI_MODULE_PATH="$PROJECT_ROOT/modules/colpali"

echo "Project root: $PROJECT_ROOT"
echo "ColPali module path: $COLPALI_MODULE_PATH"

# Change to the colpali module directory
cd "$COLPALI_MODULE_PATH"

# Run the endpoint deployment
echo "Deploying ColPali models to Azure ML Online Endpoint..."
echo "This will deploy the registered models to the inference endpoint created by Bicep."

uv run scripts/deploy_endpoint.py

if [ $? -ne 0 ]; then
    echo "Endpoint deployment failed" >&2
    exit 1
fi

echo "Endpoint deployment completed successfully!"
echo "ColPali models are now available for inference via the online endpoint."
