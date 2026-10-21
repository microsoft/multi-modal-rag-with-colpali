# ColPali Inference Service

Late-interaction embeddings using
[`TomoroAI/tomoro-ai-colqwen3-embed-4b-awq`](https://huggingface.co/TomoroAI/tomoro-ai-colqwen3-embed-4b-awq):
a W4A16-quantized ColQwen3 model producing **320-dimensional per-token vectors**.

## Architecture

Each StatefulSet pod has a model-downloader init container, a CPU shim on port
8000, and a GPU vLLM sidecar on port 8001. The model is downloaded once to a PVC.
The shim handles tokenization, image decoding, and pooling; vLLM owns the GPU.

Images travel through shared `/shm` files, not base64 between containers. Files
remain available until upstream reads finish; interrupted requests leave cleanup
to the age-based janitor. Keep its age above the longest request timeout.
Tokenizer access is serialized per shim worker, including through cancellation;
GPU requests remain concurrent.
Image operations acquire a per-worker slot before copying or staging images.
The `LIMIT_CONCURRENCY` bound includes detached operations until they finish,
so disconnecting an HTTP caller does not release image-operation capacity.

## Runtime and deployment

Python 3.14, vLLM **0.32.0+cu129**, PyTorch 2.13, and Transformers 5.19.
The GPU image uses CUDA 12.9; the shim runs CPU operations. Dockerfiles and
`uv.lock` pin the runtime. Verify the target GPU driver; image builds do not
upgrade host drivers. Load only trusted checkpoint code and files: Accelerate's
checkpoint-path advisory GHSA-4j2p-28q2-5m79 remains unresolved.

From the repository root:

```powershell
.\scripts\apply_helm.ps1
```

## Quantized checkpoint and GPU memory

The default **0.75 GPU memory budget and zero CPU weight offload** leave activation
headroom for the unquantized vision encoder on 12 GB A10 GPUs. vLLM detects the
checkpoint's AutoRound/GPTQ packing; **do not force `--quantization awq`**.
Other models need compatible retrieval-processor helpers, prompts, and GPU settings,
not just a different `MODEL_ID`. Query and document embeddings must use the same model.

## Configuration

| Helm value under `colpaliInference` | Environment variable | Default |
|---|---|---|
| `modelId` | `MODEL_ID` | Checkpoint above |
| `maxImagePixels` | `COLPALI_MAX_IMAGE_PIXELS` | 4,194,304 |
| `vllm.maxModelLen` | `VLLM_MAX_MODEL_LEN` | 3,072 |
| `vllm.gpuMemoryUtilization` | `VLLM_GPU_MEMORY_UTILIZATION` | 0.75 |
| `vllm.cpuOffloadGb` | `VLLM_CPU_OFFLOAD_GB` | 0 |
| `uvicornWorkers` | `UVICORN_WORKERS` | 4 |
| `limitConcurrency` | `LIMIT_CONCURRENCY` | 36 |

The GPU launch uses bfloat16, eager execution, 8,192 maximum batched tokens, and
16 sequences. See [chart values](../helm/colpali-stack/values.yaml) and the
[tuning guide](../helm/colpali-stack/README.md#indexing-and-inference-tuning)
for overrides, batching, concurrency, and resource limits.

The selected [PULSAR settings](https://arxiv.org/html/2608.28572#A1) use 150-DPI
embedding images and 500-DPI stored images; rendering belongs to the
[document processor](../document_processor/README.md). The image cap must match
in the shim, vLLM, and indexer. Helm sets it in all three; vLLM supplies both
`max_pixels` and `size.longest_edge` to prevent checkpoint settings overriding it.
The 4,096-visual-token ceiling can exceed the 3,072-token sequence budget,
which also includes prompt tokens. Oversized images trigger bounded indexer
downscaling without reducing stored-image resolution. Tune against your documents
and GPU; these defaults are not a throughput or latency guarantee.

## API

| Endpoint | Purpose |
|---|---|
| `GET /health` or `/` | Ready when the shim and vLLM are healthy |
| `GET /livez` | Shim process liveness |
| `POST /embeddings` | Embed either images or texts, not both |

Image request (base64 JPEG/PNG, optionally data-URI-prefixed):

```json
{"images": ["<base64-image>"], "pooling_type": ["hierarchical_pooling", "mean_pooling"]}
```

Query request:

```json
{"texts": ["Find the revenue table"], "pooling_type": ["none"]}
```

Responses contain `batch x vectors x 320` arrays. `none` returns `embeddings`;
`hierarchical_pooling` returns `hierarchical_pooled_embeddings` (pool factor 3);
`mean_pooling` returns `mean_row_pooled_embeddings` and
`mean_column_pooled_embeddings`. Vector counts can differ by image.
Queries retain active augmentation tokens and drop only masked padding. Image
pooling retains contextual tokens; the shim checks prompt/grid alignment with vLLM.

Optional integer `priority` is forwarded to vLLM. Token overflow returns HTTP 422
with `detail.code=embedding_token_limit_exceeded`, `input_tokens`, and `max_tokens`.
Other upstream failures retain retryable 429/503/502 responses and retry headers.

## Checks

From this module, run the CPU/HTTP suite without model weights or a GPU:

```powershell
uv sync --locked
$env:PYTHONPATH = "$PWD\src"
uv run --locked python -m unittest discover -s tests -v
```

Optional checks are enabled with:

| Variable | Check |
|---|---|
| `TEST_PROCESSOR_PATH` | Trusted local processor snapshot: query augmentation and padding |
| `COLPALI_TEST_PROCESSOR_PATH` | Trusted local processor snapshot: concurrent query/image tokenization |
| `COLPALI_TEST_MODEL_DIRECTORY` | Parent of `tomoro-ai-colqwen3-embed-4b-awq`: offline processor and local HTTP round trips |
| `COLPALI_TEST_BASE_URL` | Authorized live shim endpoint: real text/image embeddings and pooling |

Offline checks do not establish GPU compatibility or retrieval quality.
