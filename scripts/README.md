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

`deploy_infra.ps1` uses the current Azure CLI subscription by default. To select a
different subscription without changing the CLI default:

```powershell
.\scripts\deploy_infra.ps1 -Subscription "your subscription name"
```

The script resolves the subscription and tenant dynamically, then derives your
tenant-local user object ID from that subscription's ARM token. This avoids using
a guest user's home-directory ID for role assignments. No identity IDs are stored
in the script, and tokens are not logged or written to disk. An interactive
`az login` user session is required. Deployment and AKS credential retrieval use
the same resolved subscription.

Resource names still come from `modules/infra/src/main.bicepparam`; changing only
`-ResourceGroup` does not change the resource base name. The script writes the
repository `.env`, updates the default kubeconfig, and applies the AKS bootstrap
manifests. Failed bootstrap commands stop the script rather than report success.

For an isolated Helm deployment, pass a separate configuration file and kubeconfig:

```powershell
.\scripts\apply_helm.ps1 -EnvFile "C:\path\test.env" `
    -Subscription "your subscription name" -KubeConfig "C:\path\test.kubeconfig"
```

Only the specified environment file and kubeconfig are updated. Without these
options, the script retains its repository `.env` and default kubeconfig behavior.
Helm dependencies are restored from the chart lockfile.
