# Deployment Scripts

Automated deployment scripts for the ColPali multi-modal RAG solution. Scripts are available for both Windows (PowerShell) and Linux (Bash) platforms.

## Available Scripts

### Core Deployment Scripts (Run in Order)

| Script | Purpose | Description |
|--------|---------|-------------|
| **deploy_infra** | Infrastructure | Creates all Azure resources including AKS cluster, Container Registry, and supporting services |
| **build_colqwen** | ColQwen Model | Builds and pushes ColQwen inference container to Azure Container Registry |
| **build_document_processor** | Document Processor | Builds and pushes document processor container to Azure Container Registry |
| **apply_helm** | Kubernetes Deploy | Deploys the complete ColPali stack (ColQwen, Document Processor, Qdrant) to AKS using Helm charts |

## Quick Start

For complete deployment, run these four scripts in order:

```bash
# Windows
.\scripts\windows\deploy_infra.ps1
.\scripts\windows\build_colqwen.ps1
.\scripts\windows\build_document_processor.ps1
.\scripts\windows\apply_helm.ps1

# Linux
./scripts/linux/deploy_infra.sh
./scripts/linux/build_colqwen.sh
./scripts/linux/build_document_processor.sh
./scripts/linux/apply_helm.sh
```
