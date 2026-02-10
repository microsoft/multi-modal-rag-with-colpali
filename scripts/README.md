# Deployment Scripts

Automated deployment scripts for the ColPali multi-modal RAG solution.

## Available Scripts

### Core Deployment Scripts (Run in Order)

| Script | Purpose | Description |
|--------|---------|-------------|
| **deploy_infra** | Infrastructure | Creates all Azure resources including AKS cluster, Container Registry, and supporting services |
| **build_all** | Container Builds | Builds and pushes all containers to container registry |
| **apply_helm** | Kubernetes Deploy | Deploys the complete ColPali stack (ColQwen, Document Processor, Qdrant) to AKS using Helm charts |

## Quick Start

For complete deployment, run these scripts in order:

```powershell
.\scripts\deploy_infra.ps1
.\scripts\build_all.ps1
.\scripts\apply_helm.ps1
```
