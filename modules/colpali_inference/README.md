# ColPali Inference Service

GPU inference service for [ColPali](https://github.com/illuin-tech/colpali)-style late-interaction embedding models. Ships configured for **[`TomoroAI/tomoro-colqwen3-embed-4b`](https://huggingface.co/TomoroAI/tomoro-colqwen3-embed-4b)** — a 4B-parameter ColQwen3 model producing 320-dim per-token vectors. Alternative checkpoints require compatible retrieval-processor helpers and image prompts as well as vLLM support; changing `MODEL_ID` alone is not sufficient.

## Architecture: CPU shim + GPU vLLM sidecar

The service runs as a Kubernetes **StatefulSet**. Each pod has two long-running containers and an init container. vLLM runs model inference on the GPU; the CPU shim handles requests, image decoding, tokenization, pooling, and telemetry.

```text
          ┌─────────────────────────────────── Pod ──────────────────────────────────┐
          │                                                                  │
client ───▶│ colpali-inference  (CPU shim, FastAPI/uvicorn, port 8000)         │
  POST    │  │  • validates EmbedRequest                                       │
  /embeddings  │  │  • decodes base64 images → PNG bytes → /shm/<uuid>.png         │
          │  │  • calls vLLM /tokenize  (all image-prompt alignment)          │
          │  │  • calls vLLM /pooling  (augmented query IDs or image prompt)   │
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

Running FastAPI and the model together can load a separate model copy per worker, exhausting GPU memory. Sharing one model can limit concurrency. Using a sidecar avoids these problems:

- **vLLM owns the GPU.** It handles batching and inference with one process and one GPU per pod.
- **The shim is stateless and CPU-bound.** It can run `UVICORN_WORKERS` (default 4) processes against the same vLLM sidecar over `localhost`. Tokenization and pooling run across CPU cores.
- **Local tokenizer access is serialized per worker.** Query preprocessing and image-prompt validation share a lock in offloaded threads because the fast tokenizer mutates backend settings. Cancelling a request does not release that lock before tokenization finishes; waiting for it does not block the event loop. vLLM calls remain concurrent.
- **Pooling stays out of the model server.** Hierarchical and mean row/col pooling are pure NumPy/Torch math on the raw token embeddings. Doing them in the shim means we can change pooling strategies without touching vLLM.
- **Images travel as files, not JSON.** The shim writes decoded PNG images to shared memory at `/shm` and passes `file:///shm/<uuid>.png` to vLLM (`--allowed-local-media-path /shm`). This avoids re-encoding images as base64 between containers.

### Container roles

1. **`model-downloader` (initContainer)** — Runs `python -m colpali_inference.app download`. Pulls `MODEL_ID` from HuggingFace Hub into `${MODEL_DIRECTORY_PATH}/$(basename MODEL_ID)` on the shared PVC. Blocks pod startup if the download fails.
2. **`colpali-inference` (CPU shim)** — FastAPI app on port `8000`. Reads `VLLM_BASE_URL=http://localhost:8001`, `MODEL_ID`, `IMAGE_SHM_DIR=/shm`. Sets `HF_HUB_OFFLINE=1` to use weights from the PVC without contacting HuggingFace Hub.
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
  "pooling_type": ["none", "hierarchical_pooling", "mean_pooling"]
}
```

Returns structured embeddings (each field is a `batch x vectors x embedding_dim` array, present only when requested; sequence lengths can vary within a batch):
```json
{
  "embeddings": [[[0.1, 0.2, ...]]],
  "hierarchical_pooled_embeddings": [[[0.15, 0.25, ...]]],
  "mean_row_pooled_embeddings": [[[0.12, 0.22, ...]]],
  "mean_column_pooled_embeddings": [[[0.13, 0.23, ...]]]
}
```

**Query Text Processing** (per-token embeddings, without pooling):
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

**[Hierarchical token pooling](https://github.com/illuin-tech/colpali?tab=readme-ov-file#token-pooling)** reduces the number of vectors by approximately the pooling factor; vector dimensions are unchanged.

**Mean row and column pooling** (from [Qdrant's PDF retrieval tutorial](https://qdrant.tech/documentation/advanced-tutorials/pdf-retrieval-at-scale/)) compresses embeddings further for fast initial retrieval.

**Two-stage retrieval:**
1. L1 uses row/column pooled embeddings for fast candidate selection
2. L2 reranks with hierarchical pooled embeddings for accuracy

See the main README for full architecture details.

### Reference preprocessing, pooling and rollout

- Images follow the checkpoint's [retrieval processor](https://huggingface.co/TomoroAI/tomoro-colqwen3-embed-4b/blob/13517a29e8c5e408f7f2684337ed407df3acb212/processing_colqwen3.py):
  `<|im_start|>user\n<|vision_start|><|image_pad|><|vision_end|>Describe the image.<|im_end|><|endoftext|>`.
  vLLM expands the image placeholder. Tokenization and embedding share one
  request builder; `continue_final_message` preserves the explicit retrieval
  suffix instead of adding the generic chat ending.
- Raw image outputs retain every active prompt position. Hierarchical pooling
  clusters that complete sequence. Delimiters and prompt words carry contextual
  information after attention; they are not masked padding.
- Row/column pooling is an application extension: average only the visual grid,
  retaining active contextual prefix/suffix vectors separately in both outputs.
- Queries use the installed processor's `process_texts` preprocessing. ColQwen3
  appends ten active query-augmentation tokens. Keep them even though their ID
  also serves as the padding ID. Only attention-mask-zero batch padding is
  removed before sending token IDs to vLLM.
- Every image output mode checks vLLM's token IDs against the installed
  processor's retrieval prompt and patch grid. Query outputs must match the
  active input lengths. Prompt/count mismatches, non-finite vectors and
  unexpected all-zero rows fail explicitly; zero rows are not silently removed.
- **Rollout requires reindexing.** Existing image vectors produced with the
  generic chat prompt are not rewritten by deploying this API. Regenerate
  affected vectors in a separately approved migration, preserving source
  images and document/page identities. Coordinate inference and retrieval
  rollout so old and new representations are not mixed.
- This repository does not implement a query-embedding cache. If external
  consumers cache embeddings, rotate their cache namespace/version and restart
  them before switching query traffic; do not reuse old query-cache entries or
  clear unrelated cache data.
- The image-resolution cap and vLLM backend are unchanged. Matching reference
  prompt IDs is not full Hugging Face/vLLM numerical parity or a demonstrated
  relevance gain; validate those separately before production rollout.

### Regression checks

With the inference dependencies installed, run from this module's directory:

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
```

These checks cover active-token selection, rectangular spatial pooling, invalid
upstream vectors, tokenizer concurrency and cancellation, and HTTP request parity
using a local fake vLLM server. They do not require model weights or a GPU.
