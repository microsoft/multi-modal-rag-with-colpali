# ColPali Inference Service

This module provides a containerized ColPali visual document understanding model for Kubernetes deployment with optimized inference serving.

## Architecture Overview

This service is deployed as a **StatefulSet with two containers and an init container** so the GPU is only consumed by the model server, while the request shim scales out across CPU cores:

1. **InitContainer (`model-downloader`)** — Populates the per-replica PVC at `/mnt/models` with the model weights from HuggingFace Hub (`MODEL_ID`), into `${MODEL_DIRECTORY_PATH}/<basename(MODEL_ID)>`.
2. **CPU shim container (`colpali-inference`)** — FastAPI on port `8000`. Handles request validation, image decoding, tokenization and pooling math (hierarchical + mean row/col). Runs multiple uvicorn workers (`UVICORN_WORKERS`, default 4) and forwards embedding work to the vLLM sidecar.
3. **GPU sidecar (`vllm`)** — `vllm/vllm-openai:v0.19.1` serving the ColPali model on port `8001`. Exposes `/pooling` and `/tokenize`; receives images from the shim via `file://` URLs on a shared tmpfs `emptyDir` mounted at `/shm`.
4. **Persistent Storage** — Each replica gets its own Premium SSD PVC for model weights, enabling true horizontal scaling.

The shim and the sidecar communicate over `localhost`; only the shim's port `8000` is exposed via the Service.

## Deployment Architecture

```yaml
StatefulSet:
  InitContainer:    model-downloader   # populates /mnt/models from HuggingFace Hub
  Containers:
    - colpali-inference (CPU)          # FastAPI shim, port 8000
    - vllm              (GPU, 1x)      # vllm/vllm-openai:v0.19.1, port 8001
  Volumes:
    - model-storage (PVC, 30Gi RWO)    # shared between init + both containers
    - image-shm     (emptyDir, tmpfs)  # 2Gi at /shm, file:// image transport
  Scaling: Independent replicas across nodes
```

Deploy via Helm:

```powershell
# From repository root
scripts/apply_helm.ps1
```

## API Endpoints

The FastAPI server provides REST endpoints for health checks and embeddings:

### Health Check
```
GET /health   # ready: shim initialised AND vLLM sidecar is /health-OK
GET /livez    # liveness: shim process only (does not depend on vLLM)
GET /         # alias for /health
```

Returns service status and model readiness:
```json
{
  "status": "healthy",
  "model_loaded": true,
  "model_info": {
    "model_name": "vidore/colqwen2-v1.0",
    "device": "cuda:0",
  }
}
```

### Embedding Generation
```
POST /embeddings
```

**Image Processing** (with multiple pooling options):
```json
{
  "images": ["data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAA..."],
  "pooling_type": ["hierarchical", "mean_pooling"],
  "pooling_config": {"pool_factor": 2}
}
```

Returns structured embeddings:
```json
{
  "embeddings": [[[0.1, 0.2, ...]]],
  "hierarchical_pooled_embeddings": {
    "hierarchical": [[[0.15, 0.25, ...]]]
  },
  "mean_row_pooled_embeddings": {
    "mean_pooling": [[[0.12, 0.22, ...]]]
  },
  "mean_column_pooled_embeddings": {
    "mean_pooling": [[[0.13, 0.23, ...]]]
  }
}
```

**Text Processing** (single embeddings only):
```json
{
  "texts": ["Find machine learning documents"],
  "pooling_type": ["none"]
}
```

Returns query embeddings:
```json
{
  "embeddings": [[[0.1, 0.2, 0.3, ...]]]
}
```

## Embedding Pooling Options

The API supports multiple pooling strategies for different use cases:

**[Hierarchical token pooling](https://github.com/illuin-tech/colpali?tab=readme-ov-file#token-pooling)** reduces embedding dimensions by ~3x while maintaining retrieval quality.

**Mean row and column pooling** (from [Qdrant's PDF retrieval tutorial](https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/)) compresses embeddings further for fast initial retrieval.

**Two-stage retrieval:**
1. L1 uses row/column pooled embeddings for fast candidate selection
2. L2 reranks with hierarchical pooled embeddings for accuracy

See the main README for full architecture details.
