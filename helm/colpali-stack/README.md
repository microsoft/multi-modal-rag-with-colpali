# ColPali Stack Helm Chart

Simple Helm chart that deploys the complete ColPali stack using dependencies.

## Components

This chart deploys three components:
- **Qdrant** - Vector database (via dependency)
- **NGINX Ingress** - Load balancer and ingress controller (via dependency)
- **Document Processor** - ColPali document processing service

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
- **Document Processor**: Processes documents, creates embeddings via ColQwen, stores in Qdrant
- **ColQwen Inference**: AI model for generating document embeddings (CPU/GPU)
- **Qdrant**: Vector database for storing and searching embeddings
- **NGINX Ingress**: Provides external access to services

## Configuration

The chart uses Helm dependencies defined in `Chart.yaml`:
- `qdrant` from https://qdrant.github.io/qdrant-helm
- `ingress-nginx` from https://kubernetes.github.io/ingress-nginx

Values are passed from the deployment script via `--set` parameters.

## Architecture Overview

### Pod Architecture
```mermaid
graph TB
    subgraph "AKS Cluster"
        subgraph "ColPali Namespace"
            subgraph "Document Processing"
                DP[Document Processor Pod]
                DPSA[Document Processor SA<br/>Workload Identity]
            end

            subgraph "AI Inference"
                CI[ColQwen Inference Pod<br/>CPU/GPU]
                HFPV[HuggingFace Cache<br/>Persistent Volume]
            end

            subgraph "Vector Database"
                QD[Qdrant Pod]
                QDPV[Qdrant Storage<br/>Premium SSD]
            end

            subgraph "Ingress"
                IG[NGINX Ingress Pod]
            end

            subgraph "Secrets Management"
                AS[app-secrets<br/>Kubernetes Secret]
                CSI[Key Vault CSI Driver]
            end
        end
    end

    subgraph "Azure Services"
        KV[Azure Key Vault<br/>API Keys + App Insights]
        SB[Service Bus<br/>Message Queue]
        ST[Storage Account<br/>Document Blobs]
        AI[Application Insights<br/>Telemetry]
        ACR[Container Registry<br/>Docker Images]
    end

    subgraph "External"
        USER[User/Client]
    end

    %% Pod relationships
    DP --> QD
    CI --> QD
    DP --> SB
    DP --> ST
    DPSA -.-> KV
    CSI -.-> KV
    CSI --> AS
    AS --> DP
    AS --> CI
    AS --> QD
    CI --> HFPV
    QD --> QDPV

    %% External access
    USER --> IG
    IG --> QD
    IG --> DP

    %% Azure services
    DP --> AI
    CI --> AI
    ACR -.-> DP
    ACR -.-> CI
    ACR -.-> QD

    %% Styling
    classDef pod fill:#e1f5fe
    classDef storage fill:#f3e5f5
    classDef azure fill:#fff3e0
    classDef secret fill:#e8f5e8

    class DP,CI,QD,IG pod
    class QDPV,HFPV,AS storage
    class KV,SB,ST,AI,ACR azure
    class CSI,DPSA secret
```

### Network Flow
```mermaid
sequenceDiagram
    participant U as User
    participant I as NGINX Ingress
    participant Q as Qdrant
    participant D as Document Processor
    participant C as ColQwen Inference
    participant S as Service Bus
    participant K as Key Vault

    Note over K: Secure Secret Management
    K-->>D: API Keys & App Insights
    K-->>C: App Insights Connection
    K-->>Q: Authentication Keys

    Note over U,I: External Access
    U->>I: HTTP Request
    I->>Q: Route to Qdrant Dashboard

    Note over D,S: Document Processing Flow
    S->>D: New Document Message
    D->>C: Send Document for Embedding
    C-->>D: Return Embeddings
    D->>Q: Store Embeddings

    Note over U,Q: Search Flow
    U->>I: Search Request
    I->>D: Route to Document Processor
    D->>C: Generate Query Embedding
    C-->>D: Return Query Embedding
    D->>Q: Vector Search
    Q-->>D: Search Results
    D-->>I: Response
    I-->>U: Search Results
```
