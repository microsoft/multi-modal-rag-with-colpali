#!/bin/bash
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

# Build all ColPali stack containers

set -e

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Building all ColPali stack containers..."
echo "========================================"

echo
echo "[1/4] Building ColQwen Inference..."
"$SCRIPTS_DIR/build_container.sh" "ColQwen Inference" "colqwen-inference" "modules/colqwen_inference" "COLQWEN_INFERENCE_IMAGE_TAG"

echo
echo "[2/4] Building Document Processor..."
"$SCRIPTS_DIR/build_container.sh" "Document Processor" "document-processor" "modules/document_processor" "DOCUMENT_PROCESSOR_IMAGE_TAG"

echo
echo "[3/4] Building Agent API..."
"$SCRIPTS_DIR/build_container.sh" "Agent API" "agent-api" "modules/agent" "AGENT_API_IMAGE_TAG"

echo
echo "[4/4] Building Agent UI..."
"$SCRIPTS_DIR/build_container.sh" "Agent UI" "agent-ui" "modules/agent" "AGENT_UI_IMAGE_TAG" "Dockerfile.chainlit"

echo
echo "========================================"
echo "All builds completed successfully!"
echo "  - colqwen-inference"
echo "  - document-processor"
echo "  - agent-api"
echo "  - agent-ui"
