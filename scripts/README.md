# Deployment Scripts

Automated deployment scripts for the ColPali multi-modal RAG solution. Scripts are available for both Windows (PowerShell) and Linux (Bash) platforms.

## Deployment Order

Run scripts in this exact order for complete deployment:

| Order | Script | Purpose | Description |
|-------|--------|---------|-------------|
| 1 | **deploy_infra** | Infrastructure | Creates all Azure resources including ML workspace, compute clusters, endpoints, Function App, and AI Search |
| 2 | **register_model** | Model Setup | Downloads ColPali models from HuggingFace and registers them in Azure ML workspace using GPU job cluster |
| 3 | **deploy_endpoint** | Model Deployment | Deploys registered ColPali models to the online endpoint for GPU-accelerated inference |
| 4 | **deploy_index** | Search Setup | Creates and configures both Azure AI Search index and QDRANT collection with aligned schemas optimized for ColPali vector embeddings |
| 5 | **deploy_function** | Function Deploy | Deploys the document processing Function App code to Azure |

**Note**: The `deploy_infra` script automatically detects if the online endpoint already exists and skips creation on subsequent runs to preserve traffic allocation configured by model deployments.

## Container Apps Deployment

The infrastructure supports both Azure Functions and Container Apps for document processing. Container Apps include QDRANT vector database and are deployed conditionally.

### Phase 1: Deploy Infrastructure Only
```bash
# Windows
.\scripts\windows\deploy_infra.ps1

# Linux
./scripts/linux/deploy_infra.sh
```

### Phase 2: Build and Push Container Images
Build and push your container images to the Azure Container Registry before enabling container apps.

### Phase 3: Enable Container Apps and Event Grid
```bash
# Windows
.\scripts\windows\deploy_infra.ps1 -DeployContainerApps true

# Linux
./scripts/linux/deploy_infra.sh -c true
```

This will deploy:
- Container Apps Environment
- QDRANT container app with persistent storage
- Document Processor container app
- Event Grid integration for automatic document processing
- User-assigned identity with ACR pull permissions

### Deploy QDRANT Collection
```bash
# Windows
.\scripts\windows\deploy_index.ps1

# Linux
./scripts/linux/deploy_index.sh
```
