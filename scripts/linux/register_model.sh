#!/bin/bash
# ColPali Model Registration Script
# Downloads and registers ColPali models in Azure ML
# Usage: ./register_model.sh

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

# Run the model registration pipeline
echo "Running ColPali model registration pipeline..."
echo "This will download ColPali models from HuggingFace and register them in Azure ML."

uv run pipeline.py

if [ $? -ne 0 ]; then
    echo "Pipeline execution failed" >&2
    exit 1
fi

echo "Pipeline completed successfully!"
echo "ColPali models have been downloaded and registered in Azure ML."
