#!/usr/bin/env pwsh
# Copyright (c) Microsoft Corporation. Licensed under the MIT License.
# Builds all ColPali stack containers

$ErrorActionPreference = "Stop"
$s = $PSScriptRoot

Write-Host "Building all containers..."
& "$s\build_container.ps1" -ServiceName "ColPali Inference" -ImageName "colpali-inference" -Directory "modules\colpali_inference" -EnvVarName "COLPALI_INFERENCE_IMAGE_TAG"
& "$s\build_container.ps1" -ServiceName "ColPali Inference vLLM" -ImageName "colpali-inference-vllm" -Directory "modules\colpali_inference" -Dockerfile "Dockerfile.vllm" -EnvVarName "COLPALI_INFERENCE_VLLM_IMAGE_TAG"
& "$s\build_container.ps1" -ServiceName "Document Processor" -ImageName "document-processor" -Directory "modules\document_processor" -EnvVarName "DOCUMENT_PROCESSOR_IMAGE_TAG"
& "$s\build_container.ps1" -ServiceName "Agent API" -ImageName "agent-api" -Directory "modules\agent" -EnvVarName "AGENT_API_IMAGE_TAG"
& "$s\build_container.ps1" -ServiceName "Agent UI" -ImageName "agent-ui" -Directory "modules\agent" -Dockerfile "Dockerfile.chainlit" -EnvVarName "AGENT_UI_IMAGE_TAG"
Write-Host "All builds complete!"
