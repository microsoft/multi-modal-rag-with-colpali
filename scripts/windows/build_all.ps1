#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

# Build all ColPali stack containers

$ErrorActionPreference = "Stop"

$scriptsDir = $PSScriptRoot

Write-Host "Building all ColPali stack containers..."
Write-Host "========================================"

Write-Host "`n[1/4] Building ColQwen Inference..."
& "$scriptsDir\build_container.ps1" -ServiceName "ColQwen Inference" -ImageName "colqwen-inference" -Directory "modules\colqwen_inference" -EnvVarName "COLQWEN_INFERENCE_IMAGE_TAG"

Write-Host "`n[2/4] Building Document Processor..."
& "$scriptsDir\build_container.ps1" -ServiceName "Document Processor" -ImageName "document-processor" -Directory "modules\document_processor" -EnvVarName "DOCUMENT_PROCESSOR_IMAGE_TAG"

Write-Host "`n[3/4] Building Agent API..."
& "$scriptsDir\build_container.ps1" -ServiceName "Agent API" -ImageName "agent-api" -Directory "modules\agent" -EnvVarName "AGENT_API_IMAGE_TAG"

Write-Host "`n[4/4] Building Agent UI..."
& "$scriptsDir\build_container.ps1" -ServiceName "Agent UI" -ImageName "agent-ui" -Directory "modules\agent" -Dockerfile "Dockerfile.chainlit" -EnvVarName "AGENT_UI_IMAGE_TAG"

Write-Host "`n========================================"
Write-Host "All builds completed successfully!"
Write-Host "  - colqwen-inference"
Write-Host "  - document-processor"
Write-Host "  - agent-api"
Write-Host "  - agent-ui"
