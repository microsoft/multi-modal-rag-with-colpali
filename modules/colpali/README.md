# ColQwen2 Model Deployment

This module downloads and registers the ColQwen2 visual document understanding model to Azure ML, then deploys it as an online endpoint for inference.

## What this module does

1. **Downloads ColQwen2 model** from Hugging Face (vidore/colqwen2-v1.0)
2. **Registers model in Azure ML** using an Azure ML pipeline
3. **Creates online endpoint** with GPU compute for fast inference
4. **Provides scoring script** that handles both image and text processing

## Key files

- `pipeline.py` - Downloads and registers the ColQwen2 model via Azure ML pipeline
- `src/score.py` - Scoring script that runs on the online endpoint
- `scripts/deploy_endpoint.py` - Creates the Azure ML online endpoint

## Deployment

Run after the Azure infrastructure is deployed:

```bash
# From the repo root
scripts/windows/register_model.ps1
scripts/windows/deploy_endpoint.ps1
```

This script handles both model registration and endpoint deployment. The endpoint URL is automatically added to the Function App configuration as `AML_EMBEDDING_ENDPOINT_URL`.

## Endpoint Usage

The scoring script (`src/score.py`) handles two types of requests:

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
