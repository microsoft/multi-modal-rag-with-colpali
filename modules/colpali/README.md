# ColQwen2 Kubernetes Deployment

This module provides a containerized ColQwen2 visual document understanding model for Kubernetes inference serving.

## What this module does

1. **Downloads ColQwen2 model** from Hugging Face (vidore/colqwen2-v1.0-hf) automatically on startup
2. **Uses shared HF cache** for faster subsequent downloads across pods
3. **Serves inference requests** via FastAPI with local storage for optimal performance
4. **Handles both image and text processing** for document understanding workflows

## Key files

- `Dockerfile` - Single-purpose inference container
- `src/app.py` - FastAPI server entry point
- `src/inference.py` - Model initialization and inference logic (includes download)
- `src/colqwen_server.py` - FastAPI server for inference

## Deployment

Deploy via Helm after infrastructure is ready:

```bash
# From the repo root
scripts/windows/apply_helm.ps1
```

## How it works

- Container starts as inference server
- Model downloads automatically if not present locally (first startup: ~5-10 minutes)
- Subsequent pods use shared HuggingFace cache (startup: ~1-3 minutes)
- Model runs from local ephemeral storage for maximum performance

## API Usage

The FastAPI server (`src/colqwen_server.py`) provides REST endpoints for embeddings:

### Image Embedding Processing
For document indexing, send base64-encoded images:

```json
{
  "images": [
    "base64_encoded_image_1",
    "base64_encoded_image_2"
  ]
}
```

Returns embeddings for each image:

```json
{
  "embeddings": [[...], [...]],
  "num_images": 2,
  "embedding_dim": 128,
  "model": "vidore/colqwen2-v1.0",
  "input_type": "images",
  "status": "success"
}
```

### Text Embedding Processing
For retrieval, send text queries:

```json
{
  "texts": [
    "Find documents about machine learning algorithms",
    "Show me neural network architectures"
  ]
}
```

Returns query embeddings:

```json
{
  "embeddings": [[...], [...]],
  "num_texts": 2,
  "embedding_dim": 128,
  "model": "vidore/colqwen2-v1.0",
  "input_type": "texts",
  "status": "success"
}
```
