# Multi-Modal RAG with ColPali on Azure Kubernetes Service (AKS)

[![CI](https://github.com/microsoft/multi-modal-rag-with-colpali/actions/workflows/ci.yml/badge.svg)](https://github.com/microsoft/multi-modal-rag-with-colpali/actions/workflows/ci.yml)

> [!NOTE]
>
> This is an **accelerator** to help you get started with multi-modal RAG using [ColPali](https://github.com/illuin-tech/colpali). ColPali was created by the research team at [Illuin Technology](https://www.illuin.tech/) — this repository provides Azure infrastructure, deployment scripts, and integration patterns to operationalize their work.

> [!WARNING]
>
> This code is provided as an accelerator implementation and should be carefully reviewed and adjusted before being used in your environments. This is a demonstration, and is **not a production ready solution**.

This repository provides a multi-modal RAG (Retrieval-Augmented Generation) solution that processes documents visually using late interaction embedding techniques. Unlike traditional approaches that compress entire documents into single vectors, late interaction methods preserve fine-grained token information—each document page is represented as an image and embedded to produce hundreds of token-level embeddings. This means query tokens can be compared against document tokens directly, capturing layout, charts, tables, and visual elements that OCR pipelines typically lose.

This repository uses **ColPali**, but any late interaction embedding model can be substituted.

## Vision Language Models & ColPali

This solution offers an alternative approach to traditional multi-modal RAG implementations by leveraging Vision Language Models (VLMs). Unlike conventional methods that require complex preprocessing pipelines, VLMs process documents holistically as images, significantly reducing system complexity.

**Why vision models over traditional text extraction?**

Traditional approaches require complex chunking strategies, OCR preprocessing (with its accuracy issues), image verbalization, and separate handling for text, tables, and visual elements. Vision Language Models process documents directly as images, understanding visual layouts and relationships without chunking or OCR. Everything—text, tables, charts, diagrams—is handled uniformly.

### What is ColPali?

ColPali is a multi-modal document understanding model that processes documents as images rather than extracted text. Unlike traditional text-only approaches, ColPali generates embeddings that capture both textual content and visual layout information.

![ColPali Architecture](https://github.com/illuin-tech/colpali/raw/main/assets/colpali_architecture.webp)
*ColPali Architecture - Image source: [illuin-tech/colpali](https://github.com/illuin-tech/colpali) repository*

**Key characteristics:**
- **Visual Processing**: Processes document pages as images, preserving layout and formatting
- **Page-Level Embeddings**: Generates vector representations for entire document pages
- **Multi-Modal**: Understands both text and visual elements like tables, charts, and document structure
- **No OCR Required**: Bypasses text extraction preprocessing steps

This implementation ships with **[`TomoroAI/tomoro-colqwen3-embed-4b`](https://huggingface.co/TomoroAI/tomoro-colqwen3-embed-4b)** — a 4B-parameter ColQwen3-based late-interaction embedding model (320-dim per-token vectors). The inference service runs the model behind a [vLLM](https://github.com/vllm-project/vllm) sidecar (`/pooling` endpoint) so the GPU only does the forward pass while a CPU FastAPI shim handles tokenization, image staging, hierarchical and mean row/column pooling. Any other ColQwen2/ColQwen3 late-interaction checkpoint compatible with vLLM's pooling task can be substituted by changing `colpaliInference.modelId` in the Helm values.

ColPali was introduced in the paper ["ColPali: Efficient Document Retrieval with Vision Language Models"](https://arxiv.org/abs/2407.01449) by Manuel Faysse, Hugues Sibille, Tony Wu, et al. (2024).

## What's Included

This is a complete end-to-end deployment for Azure. The main complexity is hosting a model serving layer and building a custom indexing pipeline—both are handled here.

**Components:**
- Event-driven document processing pipeline (Blob Storage → Event Grid → Service Bus)
- ColQwen3 (`TomoroAI/tomoro-colqwen3-embed-4b`) inference service on AKS — vLLM GPU sidecar + CPU pooling shim, with model weights cached on a shared PVC
- Qdrant vector database for similarity search
- Complete infrastructure as code (Bicep templates)
- Docker images and Helm charts for all services
- Agent API and UI for querying

### Architecture Overview

#### Event-Driven Document Processing
1. **PDF Upload** → Users upload documents to Azure Blob Storage
2. **Event Trigger** → Storage generates blob events, routed by Event Grid to Service Bus
3. **Async Processing** → Document Processor consumes queue messages and reads documents
4. **Image Extraction** → Documents converted to high-resolution page images
5. **AI Inference** → `tomoro-colqwen3-embed-4b` (served via vLLM) generates multi-modal embeddings on AKS pods
6. **Image Storage** → Page images uploaded to Azure Blob Storage for retrieval
7. **Vector Storage** → Embeddings stored in Qdrant with metadata and image URLs

#### Query & Retrieval
8. **User Queries** → Submitted via NGINX Ingress to Qdrant vector database
9. **Semantic Search** → Vector similarity search returns relevant document sections with image URLs
10. **Image Retrieval** → Page images fetched from Azure Blob Storage using stored URLs
11. **RAG Integration** → Results with images consumed by AI Foundry models for intelligent responses

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
flowchart TB
    subgraph INFRA_LAYER ["Azure Infrastructure Layer"]
        direction LR
        INFRA[Infrastructure Deployment<br/>Bicep Templates] --> AKS[AKS Cluster]
        INFRA --> STORAGE[Blob Storage]
        INFRA --> EG[Event Grid]
        INFRA --> SB[Service Bus]
        INFRA --> KV[Key Vault]
        INFRA --> ACR[Container Registry]
    end

    subgraph K8S_LAYER ["AKS Cluster - colpali Namespace"]
        direction TB

        subgraph MODEL_SETUP ["Model Setup"]
            HF[HuggingFace Hub] --> INIT[Init Container<br/>Model Download]
            INIT --> PVC[Persistent Volume<br/>Model Cache]
        end

        subgraph APP_SERVICES ["Application Services"]
            COLQWEN[tomoro-colqwen3-embed-4b Inference<br/>StatefulSet (vLLM + shim)]
            DOCPROC[Document Processor<br/>Deployment]
            QDRANT[Qdrant Vector DB<br/>StatefulSet]
            AGENT_API[Agent API<br/>Deployment]
            AGENT_UI[Agent UI<br/>Deployment]
            INGRESS[NGINX Ingress<br/>External Access]
        end

        PVC -.->|Mount Model| COLQWEN
    end

    %% Event-Driven Processing Flow (integrated with existing nodes)
    USER[User] -->|1. Upload PDF| STORAGE
    STORAGE -->|2. Blob Event| EG
    EG -->|3. Route Event| SB
    SB -->|4. Queue Message| DOCPROC
    DOCPROC -->|5. Read Document| STORAGE
    DOCPROC -->|6. Generate Embeddings| COLQWEN
    COLQWEN -->|7. Multi-Modal Embeddings| DOCPROC
    DOCPROC -->|8. Store Page Images| STORAGE
    DOCPROC -->|9. Store Vectors + URLs| QDRANT

    subgraph QUERY_LAYER ["Query & Retrieval"]
        direction LR
        QUERY[User Query] --> INGRESS
        INGRESS --> AGENT_UI
        AGENT_UI --> AGENT_API
        AGENT_API --> QDRANT
        QDRANT -->|Vector Search| RESULTS[Relevant Documents]
        RESULTS --> AGENT_API
    end

    %% Infrastructure connections
    ACR -.->|Pull Images| COLQWEN
    ACR -.->|Pull Images| DOCPROC
    ACR -.->|Pull Images| QDRANT
    ACR -.->|Pull Images| AGENT_API
    ACR -.->|Pull Images| AGENT_UI
    KV -.->|Secrets & Config| DOCPROC
    KV -.->|Secrets & Config| COLQWEN
    KV -.->|Secrets & Config| AGENT_API

    %% Node Styling
    classDef azure fill:#e8f4f8,stroke:#0078d4,stroke-width:2px
    classDef k8s fill:#f0f8ff,stroke:#326ce5,stroke-width:2px
    classDef flow fill:#f0fff0,stroke:#28a745,stroke-width:2px
    classDef query fill:#f8f0ff,stroke:#6f42c1,stroke-width:2px

    class INFRA,AKS,STORAGE,EG,SB,KV,ACR azure
    class INIT,PVC,COLQWEN,DOCPROC,QDRANT,AGENT_API,AGENT_UI,INGRESS k8s
    class USER,STORAGE,EG,SB,DOCPROC,COLQWEN,QDRANT flow
    class QUERY,INGRESS,AGENT_UI,AGENT_API,RESULTS query
```



For detailed component descriptions, deployment topology, and technical specifications, see the **[Infrastructure Guide](modules/infra/README.md)**.

## Why Qdrant Vector Database + Azure Kubernetes Service?

### Why Qdrant Vector Database over AI Search for ColPali specific scenarios?
- **Multi-Vector Limits**: AI Search has a 100 multi-vector limit per document, while Qdrant has no such restriction - this is needed to use ColPali's based models that output over 100 embeddings per document.
- **Advanced Multi-Vector Operations**: Qdrant supports reranking and MAX_SIM operations that we cannot perform natively in AI Search. This makes it a lot easier, to implement late interaction where we compare multi-embeddings stored, with multi-embeddings at query time.

For this specific case, AI Search would not have worked for our use case, but it has great applications in other scenarios.

> [!NOTE]
>
> AI Search does offer other ways to achieve multi-modal RAG (See [Multimodal search in Azure AI Search](https://learn.microsoft.com/en-us/azure/search/multimodal-search-overview)), but for the use case we explored, ColPali based approaches led to higher retrieval quality, and higher indexing throughput at scale in production based on our benchmarking. Results will vary from use case to use case and should be benchmarked accordingly.

### Why AKS over Container Apps?
- **Managed Disk Support**: Qdrant requires persistent managed disk storage (not NFS volumes) for optimal performance per Qdrant's recommendations. This is not possible with other container based setups on Azure at the time of experimentation.
- **Simpler Setup**: No need to setup multiple Azure Services to host the different services, everything can run inside the same AKS cluster.
- **Shared Compute Costs**: Multiple services (document processor + ColQwen3 inference) share the same node pool.

## Project Structure

```
├── modules/
│   ├── agent/          # RAG agent application
│   ├── colpali_inference/   # tomoro-colqwen3-embed-4b inference service (vLLM sidecar + CPU shim)
│   ├── document_processor/  # FastAPI document processing service
│   ├── helm/           # Helm charts for AKS deployment
│   └── infra/          # Bicep infrastructure templates
└── scripts/            # Deployment automation
```

## Scalability Optimizations

Two techniques make ColPali embeddings practical at scale:

**[Hierarchical token pooling](https://github.com/illuin-tech/colpali?tab=readme-ov-file#token-pooling)** (from the ColPali team) reduces embedding dimensions by ~3x while maintaining retrieval quality.

**Mean row and column pooling** (from [Qdrant's PDF retrieval tutorial](https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/)) compresses embeddings further for fast initial retrieval.

**Two-stage retrieval:**
1. L1 uses row/column pooled embeddings for fast candidate selection
2. L2 reranks with hierarchical pooled embeddings for accuracy

**Chosen approach:** We use row/column mean pooling for L1 and hierarchical pooling for L2 with quantized prefetch (`mean_pooling_with_hierarchical_quantized_prefetch_only`) based on benchmarking results. We did extensive internal benchmarking, and determined that this combination, has the best latency / retrieval quality trade off for production deployments.

## Quick Start

Ready to deploy? See the **[scripts/README.md](scripts/README.md)** for complete deployment instructions and automation scripts.

> [!WARNING]
>
> This code is provided as an accelerator implementation and should be carefully reviewed and adjusted before being used in your environments. This is a demonstration, and is **not a production ready solution**.

### Prerequisites
- Azure subscription
- Azure CLI
- Python 3.11+

## Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details on:

- Setting up pre-commit hooks for automatic code quality checks
- Code standards and linting requirements
- Submitting pull requests

Quick start:
1. Fork the repository
2. Install pre-commit hooks: `pip install pre-commit && pre-commit install`
3. Create a feature branch
4. Make your changes (hooks will run automatically on commit)
5. Submit a pull request

## License

MIT License - see [LICENSE](LICENSE) for details.
