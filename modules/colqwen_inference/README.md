# ColQwen Inference Service

This module provides a containerized ColQwen2 visual document understanding model for Kubernetes deployment with optimized inference serving.

## Architecture Overview

This service uses a **StatefulSet with InitContainer pattern** for optimized model deployment:

1. **InitContainer** - Downloads ColQwen2 model (vidore/colqwen2-v0.2) to persistent volume
2. **Main Container** - Serves inference requests via FastAPI using pre-downloaded model
3. **Persistent Storage** - Each pod gets its own Premium SSD volume for model storage
4. **Horizontal Scaling** - Each replica downloads its own model copy, enabling true multi-node scaling

## Key Components

- `src/app.py` - FastAPI server with dual modes (download/serve)
- `src/inference.py` - ColQwen2 model initialization and inference logic
- `src/models.py` - Pydantic models for API requests/responses
- `src/logging.py` - Telemetry and structured logging
- `Dockerfile` - Multi-purpose container (InitContainer + inference server)

## Deployment Architecture

```yaml
StatefulSet:
  InitContainer: Downloads model to PVC
  MainContainer: Serves inference from PVC
  Storage: Premium SSD per replica (30Gi default)
  Scaling: Independent replicas across nodes
```

Deploy via Helm:

```bash
# From repository root
scripts/windows/apply_helm.ps1
```

## API Endpoints

The FastAPI server provides REST endpoints for health checks and embeddings:

### Health Check
```
GET /health
GET /
```

Returns service status and model readiness:
```json
{
  "status": "healthy",
  "model_loaded": true,
  "model_info": {
    "model_name": "vidore/colqwen2-v0.2",
    "device": "cuda:0",
    "architecture": "ColQwen2"
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

## Scalability Optimizations

This implementation incorporates two key optimisations to enable efficient scaling of ColPali embeddings:

### Hierarchical Token Pooling

We utilize [hierarchical token pooling](https://github.com/illuin-tech/colpali?tab=readme-ov-file#token-pooling) developed by the ColPali team to reduce embedding dimensions by approximately 3x. This technique significantly decreases storage requirements and computational overhead while preserving retrieval quality.

### Mean Row and Column Pooling

We implement mean row and column pooling techniques, suggested by the Qdrant team from their [PDF retrieval at scale tutorial](https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/), which further compress embeddings for efficient initial retrieval stages.

### Two-Stage Retrieval Architecture

By combining both optimization techniques, we achieve a hybrid approach:

1. **L1 Retrieval**: Uses mean row and column pooled embeddings for fast initial candidate selection
2. **L2 Reranking**: Applies hierarchical pooled embeddings for precise final ranking

This two-stage architecture balances retrieval speed with accuracy, enabling scalable deployment while maintaining high-quality results.
