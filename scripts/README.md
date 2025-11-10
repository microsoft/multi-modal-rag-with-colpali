# Deployment Scripts

Automated deployment scripts for the ColPali multi-modal RAG solution. Scripts are available for both Windows (PowerShell) and Linux (Bash) platforms.

## Available Scripts

### Core Deployment Scripts (Run in Order)

| Script | Purpose | Description |
|--------|---------|-------------|
| **deploy_infra** | Infrastructure | Creates all Azure resources including ML workspace, compute clusters, AKS cluster, and supporting services |
| **register_model** | Model Setup | Downloads ColPali models from HuggingFace and registers them in Azure ML workspace using GPU job cluster |
| **deploy_endpoint** | Model Deployment | Deploys registered ColPali models to the online endpoint for GPU-accelerated inference |
| **build_document_processor** | Container Build | Builds and pushes document processor container to Azure Container Registry |
| **apply_helm** | Kubernetes Deploy | Deploys Qdrant and document processor to AKS using Helm charts |

**Note**: The `deploy_infra` script automatically detects if the online endpoint already exists and skips creation on subsequent runs to preserve traffic allocation configured by model deployments.

## Quick Start

For complete deployment, run these five scripts in order:

```bash
# Windows
.\scripts\windows\deploy_infra.ps1
.\scripts\windows\register_model.ps1
.\scripts\windows\deploy_endpoint.ps1
.\scripts\windows\build_document_processor.ps1
.\scripts\windows\apply_helm.ps1

# Linux
./scripts/linux/deploy_infra.sh
./scripts/linux/register_model.sh
./scripts/linux/deploy_endpoint.sh
./scripts/linux/build_document_processor.sh
./scripts/linux/apply_helm.sh
```
