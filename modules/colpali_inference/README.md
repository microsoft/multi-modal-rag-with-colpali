# ColPali Inference Service

GPU inference service for [ColPali](https://github.com/illuin-tech/colpali)-style late-interaction embedding models. Ships configured for **[`TomoroAI/tomoro-colqwen3-embed-4b`](https://huggingface.co/TomoroAI/tomoro-colqwen3-embed-4b)** — a 4B-parameter ColQwen3 model producing 320-dim per-token vectors — but any HuggingFace checkpoint that vLLM can load with `--task embed` will work by changing `MODEL_ID`.

## Architecture: CPU shim + GPU vLLM sidecar

The service runs as a Kubernetes **StatefulSet** with two long-running containers in a single pod plus a one-shot init container. The split exists so the GPU is only doing the model forward pass while everything else (JSON parsing, base64 decoding, tokenization plumbing, pooling math, telemetry) scales horizontally across CPU cores.

```text
          ┌─────────────────────────────────── Pod ──────────────────────────────────┐
          │                                                                  │
client ───▶│ colpali-inference  (CPU shim, FastAPI/uvicorn, port 8000)         │
  POST    │  │  • validates EmbedRequest                                       │
  /embed  │  │  • decodes base64 images → PNG bytes → /shm/<uuid>.png         │
          │  │  • calls vLLM /tokenize  (text path)                            │
          │  │  • calls vLLM /pooling  (text and image paths)                  │
          │  │  • hierarchical / mean-row / mean-col pooling on CPU            │
          │  │                                                                 │
          │  └── localhost:8001 ──▶ vllm  (GPU, vllm/vllm-openai:v0.19.1)        │
          │                            • loads model from /mnt/models/offline   │
          │                            • --served-model-name = MODEL_ID         │
          │                            • reads image bytes from file:///shm/... │
          │                                                                  │
          │  shared volumes:                                                  │
          │    /mnt/models   PVC      (model weights, populated by init)      │
          │    /shm         tmpfs    (image transport, file:// URLs)          │
          └──────────────────────────────────────────────────────────────────────────────┘
```

### Why a sidecar, not a single GPU process?

In the naive single-process design (FastAPI + transformers on GPU), every uvicorn worker either pins its own GPU copy of the model (out of memory) or serializes through one shared model (no concurrency). Splitting responsibilities means:

- **vLLM owns the GPU.** Continuous batching, paged attention and the `embed` task implementation already exist in vLLM; we don't reinvent them. One vLLM process per pod, one GPU per pod.
- **The shim is stateless and CPU-bound.** It can run `UVICORN_WORKERS` (default 4) processes against the same vLLM sidecar over `localhost`. Tokenization and pooling parallelize across cores instead of fighting the GPU.
- **Pooling stays out of the model server.** Hierarchical and mean row/col pooling are pure NumPy/Torch math on the raw token embeddings. Doing them in the shim means we can change pooling strategies without touching vLLM.
- **Images travel as files, not JSON.** The shim writes decoded PNG bytes to a tmpfs `emptyDir` at `/shm` and passes `file:///shm/<uuid>.png` to vLLM (vLLM's `--allowed-local-media-path /shm`). This avoids the cost of re-encoding images as base64 over the loopback HTTP hop.

### Container roles

1. **`model-downloader` (initContainer)** — Runs `python -m src.colpali_inference.app download`. Pulls `MODEL_ID` from HuggingFace Hub into `${MODEL_DIRECTORY_PATH}/$(basename MODEL_ID)` on the shared PVC. Fails the pod fast if the model can't be fetched.
2. **`colpali-inference` (CPU shim)** — FastAPI app on port `8000`. Reads `VLLM_BASE_URL=http://localhost:8001`, `MODEL_ID`, `IMAGE_SHM_DIR=/shm`. Sets `HF_HUB_OFFLINE=1` so the shim never tries to phone HF Hub at runtime — weights come from the PVC.
3. **`vllm` (GPU sidecar)** — `vllm/vllm-openai:v0.19.1`. Launch flags (`--task embed`, `--served-model-name`, `--max-model-len`, `--gpu-memory-utilization`, `--allowed-local-media-path /shm`, etc.) are baked into `Dockerfile.vllm`'s ENTRYPOINT so the Helm chart only injects env overrides. `--served-model-name` is set to `MODEL_ID` so requests carrying `{"model": "TomoroAI/..."}` resolve correctly against weights stored under the on-disk basename.

### Why a StatefulSet (not Deployment)?

Each replica gets its own `volumeClaimTemplate`-backed PVC for the model. A new pod re-uses the same PVC across rolling restarts (no re-download), and replicas scale out cleanly because they don't share `RWO` storage.

Deploy via Helm:

```powershell
# From repository root
scripts/apply_helm.ps1
```

See [modules/helm/colpali-stack/values.yaml](../../modules/helm/colpali-stack/values.yaml) (the `colpaliInference:` block) for the full set of tunables: `modelId`, `replicaCount`, `uvicornWorkers`, `limitConcurrency`, `vllm.maxModelLen`, `vllm.gpuMemoryUtilization`, GPU/CPU resource requests, `nodePool`, `spotNodePool`, etc.

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
    "model_name": "TomoroAI/tomoro-colqwen3-embed-4b",
    "vllm_base_url": "http://localhost:8001"
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
  "pooling_type": ["hierarchical_pooling", "mean_pooling"]
}
```

Returns structured embeddings (each field is a `batch x patches x embedding_dim` array, present only when requested):
```json
{
  "embeddings": [[[0.1, 0.2, ...]]],
  "hierarchical_pooled_embeddings": [[[0.15, 0.25, ...]]],
  "mean_row_pooled_embeddings": [[[0.12, 0.22, ...]]],
  "mean_column_pooled_embeddings": [[[0.13, 0.23, ...]]]
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
