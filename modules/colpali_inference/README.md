# ColPali Inference Service

GPU inference service for [ColPali](https://github.com/illuin-tech/colpali)-style late-interaction embedding models. Ships configured for **[`TomoroAI/tomoro-ai-colqwen3-embed-4b-awq`](https://huggingface.co/TomoroAI/tomoro-ai-colqwen3-embed-4b-awq)** — a W4A16-quantized, 4B-parameter ColQwen3 model producing 320-dim per-token vectors. Alternative checkpoints require compatible retrieval-processor helpers and image prompts as well as vLLM support; changing `MODEL_ID` alone is not sufficient.

## Quantized checkpoint and GPU memory

Only the language-model linear layers are quantized; the vision encoder and
embedding projection remain unquantized. All model weights stay on the GPU.
CPU image preparation, tokenization and output pooling are not model-weight
offloading and remain part of the service.

Despite the `-awq` repository name, the published checkpoint's
[`config.json`](https://huggingface.co/TomoroAI/tomoro-ai-colqwen3-embed-4b-awq/blob/309474f5f30f6d8631457680ec640557fe8ad573/config.json)
declares `quant_method: auto-round`, `packing_format: auto_round:auto_gptq`,
4 bits and group size 128. vLLM handles this through its native AutoRound
quantization support, which can select GPTQ Marlin kernels on supported
GPUs. Leave quantization auto-detection enabled: **do not force
`--quantization awq`, rewrite the packing metadata, or install AutoAWQ in the
CPU shim**. The downloader's tied-embedding config fix preserves this metadata.

The publisher reports approximately 3.5 GB model memory versus 8.4 GB for the
original checkpoint, not a peak-runtime VRAM requirement. Vision activations,
input resolution and concurrent requests need additional headroom. The target
is a 12 GB A10 GPU with no CPU weight offload; verify startup, image and text
embeddings, peak VRAM and sustained mixed traffic on that hardware before
rollout. Smaller weights alone do not establish a latency or throughput gain.
Keep the CUDA 12.9-compatible vLLM 0.32.0 image on driver-570 deployments.

**Existing deployments:** rebuild the inference images and set
`colpaliInference.modelId` to the quantized ID explicitly if saved Helm values
still select the original checkpoint. The init container downloads into a new
basename directory on the PVC; it does not overwrite the original model cache.
Allow disk space for both snapshots during the transition. Remove any local
CPU-offload launch overrides rather than carrying them into the new deployment.

Quantization changes numerical embeddings even though their dimension remains
320. Reindex into a separate collection, validate retrieval quality, then switch
query inference and the index together; do not silently mix original and
quantized vectors. Keep the original model/index available for rollback and
invalidate any external query-embedding caches. Deploying the new model does
not migrate existing Qdrant vectors.

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
          │  └── localhost:8001 ──▶ vllm  (GPU, vLLM 0.32.0 / CUDA 12.9)       │
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
- **Pooling stays out of the model server.** Hierarchical and mean row/col pooling are pure NumPy/Torch math on the raw token embeddings. Doing them in the shim means we can change pooling strategies without touching vLLM.
- **Images travel as files, not JSON.** The shim writes decoded PNG images to shared memory at `/shm` and passes `file:///shm/<uuid>.png` to vLLM (`--allowed-local-media-path /shm`). This avoids re-encoding images as base64 between containers.

### Container roles

1. **`model-downloader` (initContainer)** — Runs `python -m colpali_inference.app download`. Pulls `MODEL_ID` from HuggingFace Hub into `${MODEL_DIRECTORY_PATH}/$(basename MODEL_ID)` on the shared PVC. Blocks pod startup if the download fails.
2. **`colpali-inference` (CPU shim)** — FastAPI app on port `8000`. Reads `VLLM_BASE_URL=http://localhost:8001`, `MODEL_ID`, `IMAGE_SHM_DIR=/shm`. Sets `HF_HUB_OFFLINE=1` to use weights from the PVC without contacting HuggingFace Hub.
3. **`vllm` (GPU sidecar)** — vLLM `0.32.0+cu129` on Python 3.14, installed on `mcr.microsoft.com/azureml/openmpi5.0-cuda12.8-ubuntu24.04:20261018.v1`. Launch flags (`--served-model-name`, `--max-model-len`, `--gpu-memory-utilization`, `--allowed-local-media-path /shm`, etc.) are baked into `Dockerfile.vllm`'s ENTRYPOINT so the Helm chart only injects env overrides. The pooling task and checkpoint quantization are auto-detected. `--served-model-name` is set to `MODEL_ID` so requests carrying `{"model": "TomoroAI/..."}` resolve correctly against weights stored under the on-disk basename.

### Runtime upgrade requirements

Both containers use Python 3.14, the newest minor version supported by vLLM
0.32.0 (`>=3.11,<3.15`). The shim's PyTorch, Transformers, ColPali engine, and
Hugging Face Hub dependencies have also been upgraded for Python 3.14.

The GPU image no longer inherits the upstream vLLM container: it installs the
pinned CUDA 12.9 vLLM wheel in `/opt/vllm` on an MCR CUDA 12.8 base, selecting
PyTorch from its matching `cu129` index. The build checks the PyTorch CUDA
version to reject accidental mixing with CUDA 13. The CPU image uses the
MCR Ubuntu 24.04 mirror. Both install Python using pinned uv 0.13.1.

The CUDA 12 variant preserves AKS-managed drivers on the A10 node pool instead
of introducing self-managed GRID drivers. Verify the actual driver with
`nvidia-smi` and run inference on the target GPU before rollout; CUDA minor
compatibility alone is not sufficient assurance for JIT/PTX kernels. The
application build does not install or upgrade host drivers.

Validated on an AKS NVIDIA A10-12Q (12 GiB) with managed driver 570.237:
Python 3.14.8, vLLM 0.32.0, PyTorch 2.13.0+cu129, and shim Transformers 5.19.0.
The original unquantized ColQwen3-4B checkpoint passed live text and synthetic-image
embedding checks, including all pooling modes, without CPU weight offload.
This is a functional compatibility check, not a throughput or retrieval-quality
benchmark, and does not replace validation on other GPUs or drivers.

Before production rollout, run the existing regression tests, then validate
model loading, `/health`, image and text `/pooling`, `/tokenize`, all pooling
modes, and retrieval quality on the target GPU and checkpoint. The unit tests
do not exercise CUDA or download model weights.

For an opt-in live check against the default 320-dimensional ColQwen3 model,
forward the shim's port and run from this module directory:

```powershell
$env:COLPALI_TEST_BASE_URL = "http://127.0.0.1:18080"
uv run --locked python -m unittest discover -s tests -p test_runtime_integration.py -v
Remove-Item Env:COLPALI_TEST_BASE_URL
```

These checks exercise real text and synthetic-image inference, all pooling
modes, finite 320-dimensional vectors, and normalization of raw embeddings.
They are skipped by the normal test suite unless the URL is set.

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
    "model_name": "TomoroAI/tomoro-ai-colqwen3-embed-4b-awq",
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

- Images follow the checkpoint's [retrieval processor](https://huggingface.co/TomoroAI/tomoro-ai-colqwen3-embed-4b-awq/blob/309474f5f30f6d8631457680ec640557fe8ad573/processing_colqwen3.py):
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
upstream vectors, and HTTP request parity using a local fake vLLM server. They
do not require model weights or a GPU.
