#!/bin/bash
# ColPali Index Deployment Script (AI Search + QDRANT)
# Usage: ./deploy_index.sh

set -e  # Exit on any error

# Get the absolute path of the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Get the absolute path of the project root (go up two levels: scripts/linux -> scripts -> root)
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
INDEX_MODULE_PATH="$PROJECT_ROOT/modules/index"

# Change to the index module directory
cd "$INDEX_MODULE_PATH"

# Run the unified deployment script
echo "Deploying QDRANT Collection..."
uv run scripts/deploy_qdrant.py
