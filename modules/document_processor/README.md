# Document Processor Service

Consumes Azure Blob Created/Deleted events from Service Bus, renders PDF/JPEG/PNG
documents, generates ColQwen embeddings, stores JPEG page images in Blob Storage,
and writes pages to a single configurable Qdrant collection.

## Dependency compatibility checks

Use Python 3.14 and the committed lockfile:

```shell
uv sync --locked --no-editable
uv run --locked python -m unittest discover -s tests -p test_dependency_compatibility.py -v
```

These offline-service tests cover PDF rendering, PNG/JPEG ingestion, transparent
image conversion, DPI downsampling, embedding image payloads, invalid image
rejection, and MSAL assertion signing. They do not call Azure or the inference
service. Transitive security floors are recorded in
`tool.uv.constraint-dependencies`.

The [workflow suite](#development) additionally exercises the incremental pipeline
through retrieval and message settlement.

## Selected ingestion improvements

This is a selective port of the agreed reusable capabilities, not a wholesale
copy of another deployment's APIs, operational policies, or business metadata.

| Capability | Public implementation |
|---|---|
| Complete success/failure reporting | Validate every required vector and full page coverage before completion; failures remain retryable or go to the dead-letter queue |
| Incremental indexing | File SHA-256, versioned JPEG page hashes, metadata/text refresh, and forced reindexing |
| Interrupted-document resume | Reuse only durable pages tied to the same source bytes and processing fingerprint |
| Bounded processing | Fixed page workers, bounded render queue, overlapping storage/inference I/O, and drained cancellation |
| Service Bus resilience | Rolling slots, asynchronous lock renewal, operation/handler deadlines, reconnection, and explicit settlement |
| Inference resilience | Pooled HTTP connections, strict response validation, HTTP 500/502 batch splitting, and bounded token-limit recovery |
| Qdrant storage and writes | Full-precision pooled vectors on disk, binary mean vectors in RAM, storage reconciliation, and retried durable writes |
| Text/image preparation | Markdown with raw-text fallback, independently configured storage/embedding DPI, cropping, and render-pixel bounds |
| Shared core data contract | 320 dimensions, pooled vector names, nested source/metadata, UUID5 page IDs, and extension-preserving image paths |
| Inference-port integration | Reference query/image token alignment, contextual-token pooling, request priorities, and cancellation-safe staged-image lifetime |

Intentional adaptations strengthen safety: MuPDF calls are serialized rather
than rendered concurrently across threads; Qdrant writes use `wait=True`;
unrelated payload indexes are preserved; and ordinary permanent failures remain
inspectable in the dead-letter queue rather than being silently completed.
The shared image cap is configured without editing cached checkpoint files.
Public AWQ/runtime defaults and the selected PULSAR settings replace the source
deployment's model/render defaults; equivalent capabilities do not imply
identical throughput, rendering bytes, or every optional payload field.

Office conversion, business-specific collection routing/metadata, document
priority lanes/preemption, operational process/backfill APIs, automatic backfill
or DLQ replay/drop policies, downstream result queues, and completion-filtered
retrieval are outside this port's selected scope. Generic file signatures,
operational heartbeat APIs, and priority-specific diagnostics were not selected
as separate features. Supported PDF/image decoders still validate their inputs.

## Incremental processing

1. Download the document and compute its SHA-256 file hash.
2. Skip rendering when every expected page has the completed file hash and the
   current processing fingerprint. Blob metadata is still refreshed.
3. Resume interrupted documents using per-page `indexed_with_file_hash` values.
   Only pages proven to come from the same bytes and processing configuration
   can skip rendering.
4. Render remaining pages incrementally. Versioned `jpeg1:` page hashes allow
   unchanged pages to retain their existing vectors and images. Extracted text
   is still refreshed when it changes, so OCR-only corrections do not retain
   stale text or purchase unnecessary embeddings.
5. Crop whitespace and upload changed pages at storage resolution while embedding
   requests use a separately configured, lower resolution. The storage image is
   never mutated by embedding token-limit recovery.
6. Validate all requested vector fields, dimensions, and values. Write changed
   pages in bounded batches, retrying individual Qdrant writes with `wait=True`.
7. Check full page coverage and source blob ETag, remove orphan vector pages,
   then stamp the completed file hash and refreshed metadata. Any failure leaves
   the document retryable; zero or partially written vectors are not success.

The fingerprint includes the model ID, embedding dimension, rendering/encoding
library versions, DPI, cropping settings, and `INDEXING_REVISION`. Increment
`INDEXING_REVISION` when changing model weights or inference behavior without
changing the model ID. `force_reindex=True` on `process_document_complete`, or
`"force_reindex": true` on a BlobCreated event, bypasses all skip/resume checks.

PDF text is extracted as Markdown using PyMuPDF4LLM, with a logged raw-text
fallback. PyMuPDF operations run on one dedicated worker thread because MuPDF
does not support concurrent use from multiple threads. Rendering overlaps with
embedding and storage I/O. A bounded page queue and fixed consumers limit live
page images; the original document bytes and lightweight per-page state remain
resident. Cancellation drains rendering before closing its images/documents.

`ProcessingResult.indexed_count` is the total page coverage of the successfully
completed index, including reused pages; `skipped_pages` reports reuse in this
attempt. It is not a count of newly purchased embedding calls. Both counts can
equal the page count after an unchanged-document replay.

### Relationship to PULSAR

[PULSAR section 3.3.3](https://arxiv.org/html/2608.28572#S3.SS3.SSS3)
describes separate 150-DPI embedding renders and 500-DPI stored/answer-time
renders. This implementation adopts **150 DPI for embedding and 500 DPI for
storage**, configured independently through `COLPALI_EMBEDDING_DPI` and
`COLPALI_STORAGE_DPI`. It renders once at storage resolution, then crops and downsamples
a copy for embedding; it does not perform two independent PDF renders or upscale
the embedding image. The shim and vLLM also adopt the production image cap of
4,194,304 pixels (4,096 merged visual tokens) and sequence limit of 3,072, rather
than the much larger DPI-ablation limits. These settings preserve an A4 page at
150 DPI subject to patch-grid rounding; larger pages may need token-limit
recovery. They do not establish benchmark parity. See the
[inference guide](../colpali_inference/README.md#configuration).

Bounded rendering, hash-based skips, durable-page resume, and completion checks
are implemented here. The paper's
[document-priority lanes](https://arxiv.org/html/2608.28572#S3.SS3.SSS1)
are not: this processor consumes one queue. vLLM request priorities are a separate
serving mechanism, not a document dispatcher with priority-lane preemption.

The [paper's architecture](https://arxiv.org/html/2608.28572#S3.SS2) also describes
filtering retrieval by document completion. Here, completion checks control
success reporting, queue acknowledgement, and the completed file hash; the
retriever does **not** filter on that hash. Already-written pages can be retrieved
while other pages are still processing. Completion is not an atomic publication
or snapshot guarantee. Retrieval's `indexed_only` prefetch option concerns
Qdrant segment optimization, not document completeness.

## Version-two storage contract

Default collection: `colpali-documents`. The default model is
`TomoroAI/tomoro-ai-colqwen3-embed-4b-awq`, with 320-dimensional vectors.

| Named vector | Storage | Index |
|---|---|---|
| `hierarchical_pooled` | Full-precision vectors on disk | HNSW disabled; exact reranking |
| `mean_row_pooled` | Full-precision vectors on disk, binary quantization in RAM | Candidate retrieval |
| `mean_column_pooled` | Full-precision vectors on disk, binary quantization in RAM | Candidate retrieval |

These are full-precision **pooled** representations, not an additional unpooled
patch-vector field. Binary quantization does not replace the stored full-precision
vectors; this is not a quantized-only index.

Page identity is UUID5 using `NAMESPACE_URL` and
`"{document_id}:page:{page_number}"`. Document identity is the source blob URL
without query parameters or fragments. Page images are stored at
`{source-container}-page-images/{source-path-with-extension}/page_{number}.jpg`.

Qdrant payloads contain:

```json
{
  "document_id": "https://example.blob.core.windows.net/documents/report.pdf",
  "page_number": 1,
  "page_count": 2,
  "source": {
    "blob_url": "https://example.blob.core.windows.net/documents/report.pdf",
    "container": "documents",
    "path": "report.pdf",
    "file_name": "report.pdf",
    "file_extension": ".pdf",
    "page_image_url": "https://example.blob.core.windows.net/documents-page-images/report.pdf/page_1.jpg"
  },
  "metadata": {
    "document": {"filename": "report.pdf", "file_extension": ".pdf"},
    "blob": {}
  }
}
```

Additional root fields are `page_id`, `text_content`, `indexed_at`, `file_hash`,
`indexed_with_file_hash`, `page_hash`, and `processing_fingerprint`. The agent
reads this nested contract and deduplicates by document identity plus page number.

Initialization rejects incompatible vector names/dimensions. Compatible
collections have full-precision vector storage moved to disk without deleting
data or unrelated payload indexes. Existing document IDs/payloads are also
checked before reuse. Deployment alone does not enqueue historical documents.

Writes are page-level, not an atomic document snapshot. In-process duplicate
events are serialized and source ETags are checked before completion, but
cross-replica updates to the same document are not a distributed transaction.
Route competing versions of a document to one owner if strict ordering is needed.

## Queue reliability

Both single Event Grid objects and arrays are supported. Slots refill as soon
as documents finish; a slow document does not block a whole received batch.
Receive calls, document handling, settlement, and resource cleanup have deadlines.
Lock renewal uses the asynchronous SDK. Transient failures are abandoned for
retry, while invalid documents and exhausted token-limit recovery are preserved
in the dead-letter queue. A delivery-count guard parks repeatedly unsettled
messages before opening their document bytes.

No output queues, collection routing, automatic backfill, or automatic DLQ
deletion are configured. Cancellation-resistant cleanup stops the listener
rather than reusing a slot whose old work may still be active.

The consumer accepts PDF/JPEG/PNG events, but the default infrastructure event
subscription only forwards `.pdf` paths in the `documents` container. To ingest
images, explicitly configure an appropriate event filter or send valid
BlobCreated messages to the configured queue. Re-uploading historical documents
or manually enqueueing their events is an operator action, not automatic backfill.

## Configuration

Existing `SERVICE_BUS_NAMESPACE_NAME`, `SERVICE_BUS_QUEUE_NAME`,
`DATA_STORAGE_ACCOUNT_NAME`, `COLPALI_INFERENCE_ENDPOINT`, `QDRANT_ENDPOINT`,
`QDRANT_API_KEY`, and managed-identity settings remain supported.

| Setting | Default | Purpose |
|---|---|---|
| `QDRANT_COLLECTION_NAME` | `colpali-documents` | Explicit collection, shared with retrieval |
| `COLPALI_EMBEDDING_DIMENSION` | 320 | Must match the configured inference model |
| `MODEL_ID` | `TomoroAI/tomoro-ai-colqwen3-embed-4b-awq` | Fingerprint model identity |
| `INDEXING_REVISION` | `1` | Invalidate caches after inference/weight changes |
| `COLPALI_STORAGE_DPI` | 500 | Stored PDF resolution; falls back to legacy `COLPALI_IMAGE_DPI` |
| `COLPALI_EMBEDDING_DPI` | 150 | Embedding image resolution; never upscales |
| `COLPALI_MAX_IMAGE_PIXELS` | 4194304 | Inference cap recorded in the fingerprint; match both inference containers |
| `COLPALI_CROP_WHITESPACE` | `true` | Crop changed page images before upload/embedding |
| `PDF_MAX_RENDER_PIXELS` | 75000000 | Reduce DPI for oversized PDF pages |
| `RENDER_BUFFER_MAX_PAGES` | 4 | Maximum queued rendered pages per document |
| `DOCUMENT_MAX_CONCURRENT` | 3 | Active message slots |
| `COLPALI_MAX_CONCURRENT` | 5 | Global concurrent inference requests and per-document workers |
| `COLPALI_BATCH_SIZE` | 1 | Maximum pages per embedding request, not a batch-fill minimum |
| `COLPALI_REQUEST_TIMEOUT` | 120 | Per-request inference timeout in seconds |
| `BLOB_MAX_CONCURRENT` | 250 | Concurrent page-image uploads |
| `QDRANT_MAX_CONCURRENT` | 5 | Concurrent writes |
| `QDRANT_BATCH_SIZE` | 20 | Maximum points per write |
| `QDRANT_REQUEST_TIMEOUT` | 120 | Qdrant client timeout in seconds |
| `DOCUMENT_TIMEOUT_SECONDS` | 1200 | Message handling deadline |
| `SERVICE_BUS_OPERATION_TIMEOUT_SECONDS` | 30 | Receive/settlement/close deadline |
| `SERVICE_BUS_CANCELLATION_TIMEOUT_SECONDS` | 5 | Cleanup deadline before stopping |
| `SERVICE_BUS_POISON_DELIVERY_THRESHOLD` | 10 | Park messages above this delivery count |

Concurrency limits are per processor process, not cluster-wide. With the
default two replicas and `COLPALI_MAX_CONCURRENT=5`, up to ten one-page embedding
requests can be active across replicas. Increasing `COLPALI_BATCH_SIZE` also
increases images in flight: the shim sends one upstream pooling call per image,
not one GPU operation per HTTP batch. See the
[Helm tuning guide](../helm/colpali-stack/README.md#indexing-and-inference-tuning)
for the corresponding chart values and override workflow.

## Development

From the repository root:

```powershell
uv sync --project modules\document_processor --locked
uv run --project modules\document_processor pytest modules\document_processor\tests
```

The workflow suite runs real PDF/image processing, inference HTTP transport,
Qdrant writes/queries, retrieval, and message settlement together. Only Azure
SDK boundaries and the GPU service are replaced: storage is a shared in-memory
fake and inference runs behind a loopback HTTP endpoint.

By default Qdrant runs in memory. To exercise the actual server, including
optimized binary-vector retrieval, start a disposable local instance:

```powershell
docker run --rm -d --name colpali-test-qdrant -p 127.0.0.1:46333:6333 -e QDRANT__TELEMETRY_DISABLED=true qdrant/qdrant:v1.19.1
$env:TEST_QDRANT_URL = "http://127.0.0.1:46333"
try {
    uv run --project modules\document_processor --frozen pytest modules\document_processor\tests
} finally {
    Remove-Item Env:\TEST_QDRANT_URL
    docker stop colpali-test-qdrant
}
```

The server URL must be loopback-only. Each workflow owns a uniquely named
collection and removes only that collection afterward. CI runs these workflows
against Qdrant 1.19.1, matching the processor client and Helm server versions.

For a bounded check against deployed inference, establish an authorized
loopback port-forward, set `TEST_INFERENCE_URL` to its base URL, and run only
`tests\test_deployed_inference.py`. This opt-in test submits synthetic pages and
one query, verifies retrieval and stored-image association, then checks unchanged
redelivery and a one-page update. It still replaces Azure Blob/Service Bus SDK
boundaries; it is not an Azure identity or queue-delivery test.
`TEST_QDRANT_URL` can select a separate loopback forward to a test Qdrant server,
with `TEST_QDRANT_API_KEY` supplied locally if required. Only the test's unique
collection is created/deleted. Coordinate shared GPU traffic before running it.
Optional pytest JUnit output records functional timings, not a throughput or
latency benchmark.

The quantized model with the documented larger image/sequence limits has also
passed a deployed Azure check using real workload identity, Blob Storage,
Service Bus, and Qdrant 1.17.1. That check covered full A4 indexing and image
retrieval, metadata-only redelivery, a single-page update, transient failure
and redelivery, malformed-message dead-lettering, and document deletion with
an empty queue afterward. Test data used a dedicated queue, collection, and
containers; no existing collections were modified.

Scenarios protect searchable page text/images and distinct citations, incremental
updates without unnecessary embedding calls, forced reindexing, partial-failure
recovery, malformed inference responses, bounded token recovery, source-version
changes, mixed Event Grid failures, and isolated/idempotent deletion. Focused
tests additionally cover rendering backpressure, cancellation, queue reconnects,
safe collection rejection, per-batch Qdrant retries, OCR-only corrections, and
real SDK workload-identity token exchange with the token endpoint replaced.

Prefer these outcome-based tests over tests of getters, argument forwarding,
or mock return values. Keep unit tests for consequential algorithms and failure
modes, not to reach a coverage percentage or test count. These local suites need
neither Azure credentials nor a GPU. The in-memory mode does not exercise
server-side optimization/quantization; neither mode verifies live Azure delivery
semantics or GPU model quality.
