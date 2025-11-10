# ColQwen2 Stack Helm Chart

Simple Helm chart that deploys the complete ColQwen2 multi-modal RAG stack using dependencies.

## Components

This chart deploys four main components:
- **Qdrant** - Vector database for embeddings storage (via dependency)
- **NGINX Ingress** - Load balancer and ingress controller (via dependency)
- **ColQwen2 Inference** - AI model service for generating document embeddings
- **Document Processor** - Service that processes documents and orchestrates embedding creation

## Prerequisites

1. Run infrastructure deployment: `.\scripts\windows\deploy_infra.ps1`
2. Have kubectl and helm installed

## Deploy Everything

```powershell
.\scripts\windows\apply_helm.ps1
```

This automatically:
1. Updates Helm dependencies (`helm dependency update`)
2. Deploys all components with proper configuration
3. Sets up Qdrant with Premium SSD storage
4. Configures ingress for Qdrant dashboard access

## Access Qdrant Dashboard

After deployment, get the ingress IP:
```bash
kubectl get ingress
```

Access Qdrant dashboard at: `http://<INGRESS-IP>/qdrant`

## Pods and Services
- **Document Processor**: Processes documents, creates embeddings via ColQwen2, stores in Qdrant
- **ColQwen2 Inference**: AI model for generating document embeddings (CPU/GPU optimized)
- **Qdrant**: Vector database for storing and searching embeddings
- **NGINX Ingress**: Provides external access to services and Qdrant dashboard

## Configuration

The chart uses Helm dependencies defined in `Chart.yaml`:
- `qdrant` from https://qdrant.github.io/qdrant-helm
- `ingress-nginx` from https://kubernetes.github.io/ingress-nginx

Values are passed from the deployment script via `--set` parameters.

## Architecture Overview

### Pod Architecture
```mermaid
%%{init: {
  'theme': 'base',
  'themeVariables': {
    'primaryColor': '#f5f5f5',
    'primaryTextColor': '#000000',
    'primaryBorderColor': '#333333',
    'lineColor': '#666666',
    'secondaryColor': '#f8f8f8',
    'tertiaryColor': '#fafafa',
    'background': '#ffffff',
    'mainBkg': '#f5f5f5',
    'secondBkg': '#f8f8f8',
    'tertiaryBkg': '#fafafa'
  }
}}%%
graph TB
    subgraph "AKS Cluster"
        subgraph "colpali Namespace"
            subgraph "Document Processing"
                DP[Document Processor Pod]
                DPSA[Document Processor SA<br/>Workload Identity]
            end

            subgraph "AI Inference"
                CI[ColQwen2 Inference Pod<br/>CPU/GPU Optimized]
                HFPV[Model Storage<br/>Persistent Volume<br/>HuggingFace Cache]
            end

            subgraph "Vector Database"
                QD[Qdrant Pod]
                QDPV[Vector Storage<br/>Premium SSD PVC]
            end

            subgraph "Network Access"
                IG[NGINX Ingress Controller]
            end

            subgraph "Configuration"
                AS[app-secrets<br/>Kubernetes Secret]
            end
        end
    end

    subgraph "Azure Infrastructure"
        KV[Key Vault<br/>Secrets & Config]
        EG[Event Grid<br/>Blob Events]
        SB[Service Bus<br/>Document Queue]
        ST[Blob Storage<br/>Document Files]
        AI[Application Insights<br/>Monitoring]
        ACR[Container Registry<br/>Application Images]
    end

    subgraph "External Access"
        USER[Users/Applications]
        DOCS[Document Upload]
    end

    %% Data flow (solid lines)
    DOCS -->|PDF Upload| ST
    ST -->|Blob Created Event| EG
    EG -->|Event Message| SB
    SB -->|Queue Message| DP
    DP -->|Read Document| ST
    DP -->|Inference Request| CI
    CI -->|Embeddings| DP
    DP -->|Store Vectors| QD
    USER -->|Query/Dashboard| IG
    IG -->|Route Traffic| QD

    %% Configuration/Management (dashed lines)
    DPSA -.->|Authenticate| KV
    KV -.->|Provide Secrets| AS
    AS -.->|Mount Config| DP
    AS -.->|Mount Config| CI

    %% Storage relationships (dotted lines)
    CI -.->|Cache Models| HFPV
    QD -.->|Persist Data| QDPV
    DP -.->|Read Files| ST
    DP -.->|Send Telemetry| AI
    CI -.->|Send Telemetry| AI

    %% Container registry (deployment time)
    ACR -.->|Pull Images| DP
    ACR -.->|Pull Images| CI
    ACR -.->|Pull Images| QD

    %% Node Styling
    classDef pod fill:#f0f8ff,stroke:#326ce5,stroke-width:2px
    classDef storage fill:#f8f8f8,stroke:#666666,stroke-width:2px
    classDef azure fill:#e8f4f8,stroke:#0078d4,stroke-width:2px
    classDef external fill:#f0fff0,stroke:#28a745,stroke-width:2px
    classDef network fill:#f8f0ff,stroke:#6f42c1,stroke-width:2px

    class DP,CI,QD pod
    class HFPV,QDPV,AS storage
    class KV,EG,SB,ST,AI,ACR azure
    class USER,DOCS external
    class IG,DPSA network
```

**Legend:**
- **Solid arrows**: Data/request flow
- **Dashed arrows**: Authentication/configuration
- **Dotted arrows**: Storage/monitoring relationships
