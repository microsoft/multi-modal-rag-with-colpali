# Multi-Modal RAG with ColPali on Azure Kubernetes Service (AKS)

[![CI](https://github.com/microsoft/multi-modal-rag-with-colpali/actions/workflows/ci.yml/badge.svg)](https://github.com/microsoft/multi-modal-rag-with-colpali/actions/workflows/ci.yml)

> [!TIP]
>
> For a write-up of a production deployment, with full ablation studies comparing design choices, see **[PULSAR: Pooled Unified Late-Interaction Search and Retrieval for Enterprise Visual Document RAG](https://arxiv.org/abs/2608.28572)**.

> [!WARNING]
>
> This **accelerator** defaults to [TomoroAI's](https://huggingface.co/TomoroAI) [quantized ColQwen3 model (`tomoro-ai-colqwen3-embed-4b-awq`)](https://huggingface.co/TomoroAI/tomoro-ai-colqwen3-embed-4b-awq), building on the foundational [ColPali approach and tooling](https://github.com/illuin-tech/colpali) from [Illuin Technology](https://www.illuin.tech/), including hierarchical token pooling. It is **not a production-ready solution**. Review and adjust the code before using it in your environments.

This repository provides a multi-modal RAG (Retrieval-Augmented Generation) solution that processes document pages as images. Each page produces hundreds of token-level embeddings rather than a single vector. Late interaction compares query tokens directly with these embeddings, helping retrieve content from text, charts, tables, and page layouts.

This repository uses **ColQwen3** by default; other compatible ColQwen2/ColQwen3 late-interaction embedding models can be substituted.

## Vision Language Models & ColPali

**Why vision models over traditional text extraction?**

Text extraction pipelines often need OCR, chunking, and separate handling for tables and images. Vision Language Models (VLMs) process whole pages as images, handling text, tables, charts, and diagrams together without OCR or text chunking.

### What is ColPali?

ColPali is a multi-modal document understanding model that processes documents as images rather than extracted text. Unlike traditional text-only approaches, ColPali generates embeddings that capture both textual content and visual layout information.

![ColPali Architecture](https://github.com/illuin-tech/colpali/raw/main/assets/colpali_architecture.webp)
*ColPali Architecture - Image source: [illuin-tech/colpali](https://github.com/illuin-tech/colpali) repository*

**Key characteristics:**
- **Visual Processing**: Processes document pages as images, preserving layout and formatting
- **Page-Level Embeddings**: Generates vector representations for entire document pages
- **Multi-Modal**: Understands both text and visual elements like tables, charts, and document structure
- **No OCR Required**: Bypasses text extraction preprocessing steps

This implementation ships with **[`TomoroAI/tomoro-ai-colqwen3-embed-4b-awq`](https://huggingface.co/TomoroAI/tomoro-ai-colqwen3-embed-4b-awq)** — a quantized 4B-parameter ColQwen3 model producing 320-dimensional token embeddings. [vLLM](https://github.com/vllm-project/vllm) runs inference on the GPU; a CPU service handles image preparation and pooling.

We chose quantization for performance and memory efficiency on our **12 GB A10 GPU nodes**. You can use the [non-quantized checkpoint](https://huggingface.co/TomoroAI/tomoro-colqwen3-embed-4b) or another compatible model with suitable hardware and settings. See the **[Inference Service Guide](modules/colpali_inference/README.md)** for configuration and tuning.

ColPali was introduced in the paper ["ColPali: Efficient Document Retrieval with Vision Language Models"](https://arxiv.org/abs/2407.01449) by Manuel Faysse, Hugues Sibille, Tony Wu, et al. (2024).

## What's Included

This repository includes model serving, document indexing, and deployment scripts for Azure.

**Components:**
- Event-driven, incremental document processing (Blob Storage → Event Grid → Service Bus), with bounded rendering, hash-based resume, and explicit message settlement
- Quantized ColQwen3 (`TomoroAI/tomoro-ai-colqwen3-embed-4b-awq`) inference service on AKS — vLLM GPU sidecar + CPU pooling shim, with model weights cached on a shared PVC
- Qdrant vector database for similarity search
- Complete infrastructure as code (Bicep templates)
- Docker images and Helm charts for all services
- Agent API and UI for querying

Indexing and retrieval share a **320-dimensional pooled-vector contract**.
The [processor guide](modules/document_processor/README.md) describes the selected
ingestion improvements, and the
[Helm tuning guide](modules/helm/colpali-stack/README.md#indexing-and-inference-tuning)
maps concurrency, batching, rendering, and serving controls.

### Architecture Overview

#### Event-Driven Document Processing
1. **PDF Upload** → Users upload documents to Azure Blob Storage
2. **Event Trigger** → Storage generates blob events, routed by Event Grid to Service Bus
3. **Async Processing** → Document Processor consumes queue messages and reads documents
4. **Image Extraction** → Documents converted to high-resolution page images
5. **AI Inference** → `tomoro-ai-colqwen3-embed-4b-awq` (served via vLLM) generates multi-modal embeddings on AKS pods
6. **Image Storage** → Page images uploaded to Azure Blob Storage for retrieval
7. **Vector Storage** → Embeddings stored in Qdrant with metadata and image URLs

#### Query & Retrieval
8. **User Queries** → Submitted through Agent UI to Agent API, which generates query embeddings
9. **Semantic Search** → Mean-vector candidate retrieval and hierarchical reranking in Qdrant return relevant pages with image URLs
10. **Image Retrieval** → Page images fetched from Azure Blob Storage using stored URLs
11. **RAG Integration** → AI Foundry models use retrieved pages to answer the query

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
            COLQWEN["tomoro-ai-colqwen3-embed-4b-awq Inference<br/>StatefulSet (vLLM + shim)"]
            DOCPROC[Document Processor<br/>Deployment]
            QDRANT[Qdrant Vector DB<br/>StatefulSet]
            AGENT_API[Agent API<br/>Deployment]
            AGENT_UI[Agent UI<br/>Deployment]
            INGRESS[NGINX Ingress<br/>External Access]
        end

        PVC -.->|Mount Model| COLQWEN
    end

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

    ACR -.->|Pull Images| COLQWEN
    ACR -.->|Pull Images| DOCPROC
    ACR -.->|Pull Images| QDRANT
    ACR -.->|Pull Images| AGENT_API
    ACR -.->|Pull Images| AGENT_UI
    KV -.->|Secrets & Config| DOCPROC
    KV -.->|Secrets & Config| COLQWEN
    KV -.->|Secrets & Config| AGENT_API

    classDef azure fill:#e8f4f8,stroke:#0078d4,stroke-width:2px
    classDef k8s fill:#f0f8ff,stroke:#326ce5,stroke-width:2px
    classDef flow fill:#f0fff0,stroke:#28a745,stroke-width:2px
    classDef query fill:#f8f0ff,stroke:#6f42c1,stroke-width:2px

    class INFRA,AKS,STORAGE,EG,SB,KV,ACR azure
    class INIT,PVC,COLQWEN,DOCPROC,QDRANT,AGENT_API,AGENT_UI,INGRESS k8s
    class USER,STORAGE,EG,SB,DOCPROC,COLQWEN,QDRANT flow
    class QUERY,INGRESS,AGENT_UI,AGENT_API,RESULTS query
```



For Azure resources and deployment details, see the **[Infrastructure Guide](modules/infra/README.md)**.

## Why Qdrant Vector Database + Azure Kubernetes Service?

### Why Qdrant Vector Database over AI Search for ColPali specific scenarios?
- **Multi-Vector Limits**: AI Search has a 100-vector limit per document, while Qdrant has no such restriction. ColPali-based models produce more than 100 embeddings per document.
- **Multi-Vector Operations**: Qdrant supports reranking and MAX_SIM operations that we cannot perform natively in AI Search. These let us compare query embeddings directly with document embeddings.

> [!NOTE]
>
> AI Search also supports [multi-modal RAG](https://learn.microsoft.com/en-us/azure/search/multimodal-search-overview). In our benchmarks, ColPali-based approaches gave better retrieval quality and higher indexing throughput at scale. Results vary, so benchmark your own use case.

### Why AKS over Container Apps?
- **Managed Disk Support**: Qdrant requires persistent managed disk storage (not NFS volumes) for optimal performance per Qdrant's recommendations. This is not possible with other container based setups on Azure at the time of experimentation.
- **Simpler Setup**: No need to setup multiple Azure Services to host the different services, everything can run inside the same AKS cluster.
- **Shared Compute Costs**: Multiple services (document processor + ColQwen3 inference) share the same node pool.

## Project Structure

```
├── modules/
│   ├── agent/          # RAG agent application
│   ├── colpali_inference/   # Quantized ColQwen3 inference (vLLM sidecar + CPU shim)
│   ├── document_processor/  # Incremental Service Bus document consumer
│   ├── helm/           # Helm charts for AKS deployment
│   └── infra/          # Bicep infrastructure templates
└── scripts/            # Deployment automation
```

## Scalability Optimizations

Two techniques make these token-level embeddings practical at scale:

**[Hierarchical token pooling](https://github.com/illuin-tech/colpali?tab=readme-ov-file#token-pooling)** (from the ColPali team) reduces embedding dimensions by ~3x while maintaining retrieval quality.

**Mean row and column pooling** (from [Qdrant's PDF retrieval tutorial](https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/)) compresses embeddings further for fast initial retrieval.

**Two-stage retrieval:**
1. L1 uses row/column pooled embeddings for fast candidate selection
2. L2 reranks with hierarchical pooled embeddings for accuracy

**Chosen approach:** We use row/column mean pooling for L1 and hierarchical pooling for L2 with quantized prefetch (`mean_pooling_with_hierarchical_quantized_prefetch_only`). This gave the best balance of latency and retrieval quality in our benchmarks.

## Quick Start

Follow the **[first-run guide](scripts/README.md)** to deploy, upload a PDF, ask a
question, and clean up the resources. It includes prerequisites and common failures.

Deployment requires an Azure subscription, PowerShell 7, Azure CLI, kubectl, Helm,
and Docker. Local Python development uses Python 3.14 and uv.

### Container runtimes

All application Dockerfiles use base images from Microsoft Container Registry
(`mcr.microsoft.com`). The agent API/UI, document processor, and inference shim
use the MCR Ubuntu 24.04 mirror; the GPU sidecar uses the Azure ML CUDA 12.8
Ubuntu 24.04 image with matching CUDA 12.9 vLLM/PyTorch wheels. Each installs
Python 3.14 with pinned uv 0.13.1, rather than
depending on the older Python bundled with the base OS. uv is downloaded from
Astral; Python packages come from PyPI, PyTorch's CUDA index, and the official
vLLM GitHub release; no build stages pull images
from Docker Hub or GHCR.

The Python minor version is aligned across `.python-version`, `pyproject.toml`,
the dependency locks, containers, and CI. Rebuild images with fresh base pulls
to receive OS updates. Python patch updates require a uv release that includes
the newer interpreter.

> [!IMPORTANT]
>
> The vLLM 0.32.0 sidecar deliberately uses the CUDA 12.9 variant to retain
> AKS-managed A10 drivers. Do not substitute the default CUDA 13 wheel without
> validating the node driver, JIT kernels, and inference. Rebuilding containers
> does not upgrade the host driver. See the [inference guide](modules/colpali_inference/README.md).

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
