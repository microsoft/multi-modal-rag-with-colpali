# ColPali Inference Service

This module provides a containerized ColPali visual document understanding model for Kubernetes deployment with optimized inference serving.

## Architecture Overview

This service uses a **StatefulSet with InitContainer pattern** for optimized model deployment:

1. **InitContainer** - Downloads ColPali style model to persistent volume (configurable via MODEL_ID)
2. **Main Container** - Serves inference requests via FastAPI using pre-downloaded model
3. **Persistent Storage** - Each pod gets its own Premium SSD volume for model storage
4. **Horizontal Scaling** - Each replica downloads its own model copy, enabling true multi-node scaling

## Deployment Architecture

```yaml
StatefulSet:
  InitContainer: Downloads model to PVC
  MainContainer: Serves inference from PVC
  Storage: Premium SSD per replica (30Gi default)
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
GET /health
GET /
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
