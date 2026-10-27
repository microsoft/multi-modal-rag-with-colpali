# Document Processor Service

Consumes Azure Blob Created/Deleted events from Service Bus, renders PDF/JPEG/PNG
documents, generates ColQwen embeddings, stores JPEG page images in Blob Storage,
and writes pages to a single configurable Qdrant collection.

## Incremental processing

1. Download the document and compute its SHA-256 file hash.
2. Skip rendering when every expected page has the completed file hash and the
   current processing fingerprint. Blob metadata is still refreshed.
3. Resume interrupted documents using per-page `indexed_with_file_hash` values.
   Only pages proven to come from the same bytes and processing configuration
   can skip rendering.
4. Render remaining pages incrementally. Versioned `jpeg1:` page hashes allow
   unchanged pages with the same effective DPI to retain their vectors and images. Extracted text
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

### Rendering

Defaults follow [PULSAR's production settings](https://arxiv.org/html/2608.28572#A1):
**150 DPI for embedding and 500 DPI for storage**, configured independently through
`COLPALI_EMBEDDING_DPI` and `COLPALI_STORAGE_DPI`. Each page is rendered once at
storage resolution, cropped, and downsampled as a copy for embedding.
The shared inference cap is 4,194,304 pixels, with a 3,072-token sequence limit.
A4 pages at 150 DPI fit; larger pages may need token-limit recovery. See the
[inference guide](../colpali_inference/README.md#configuration).

## Storage contract

Default collection: `colpali-documents`. The default model is
`TomoroAI/tomoro-ai-colqwen3-embed-4b-awq`, with 320-dimensional vectors.

| Named vector | Storage | Index |
|---|---|---|
| `hierarchical_pooled` | Full-precision vectors `cold` | HNSW disabled; exact reranking |
| `mean_row_pooled` | Full-precision vectors `cold`, binary copies `pinned` | HNSW `pinned`; candidate retrieval |
| `mean_column_pooled` | Full-precision vectors `cold`, binary copies `pinned` | HNSW `pinned`; candidate retrieval |

These are full-precision **pooled** representations, not an additional unpooled
patch-vector field. Binary quantization does not replace the stored full-precision
vectors; this is not a quantized-only index.
`rescore=False` is a query setting, not a storage-retention setting. Qdrant 1.20
does not expose a discard-originals option for this binary-quantized multivector
schema; its optimizer builds quantized data from the retained vector storage.
Deleting a named vector would remove that searchable representation, not just
its float copy. Inline storage is not a workaround: it retains originals within
the graph and does not support multivectors.

Candidate retrieval uses binary mean vectors and pinned HNSW links without
float mean-vector rescoring. The stored float mean copies remain cold; the final
reranker reads hierarchical float vectors for the candidate union. The optimized
candidate path does not need float coordinates for scoring; ingestion, rebuilding
indexes, and segments without quantized data can still use the stored floats.

Qdrant **1.20.0** uses explicit memory tiers: payload storage is `cold`, the ID
tracker and the `document_id`/`processing_fingerprint` keyword indexes are
`pinned`. `cold` and `cached` are memory-mapped and can both enter the OS page
cache; only `cached` prewarms it at startup. `pinned` allocations cannot be
evicted. These settings are not a hard memory cap or a query-result cache.
The hierarchical field also records `cached` HNSW placement, but retains `m=0`.
Cold floats and pinned binary copies preserve the previous
`on_disk=True`/`always_ram=True` layout; this is not a new 32-fold RAM reduction
from the upgrade. The deliberate changes are pinning the mean graphs and
restoring binary-only prefetch without float rescoring.

### RAM, disk, and query working set

The objective is to keep candidate retrieval resident while letting the larger
reranking representation exceed RAM. Hierarchical floats remain `cold` to avoid
proactively warming a corpus that may not fit. Frequently accessed pages still
enter the OS cache on demand; `cached` would not give them higher eviction
priority. HNSW links, binary scoring data, and float
vectors are separate structures. Each mean field's graph connects page points;
it does not create one graph node per pooled token. Pinning these relatively
small graphs prevents hierarchical-vector page-cache activity from evicting
candidate-search links. Keeping graphs `cached` would instead permit eviction
and trade some latency predictability for reclaimable RAM.

For `N` locally stored page replicas, an average of `R + C` mean-pooled vectors
per page, and `H` hierarchical vectors per page, the logical coordinate sizes are:

```text
float coordinate bytes on disk = N * (R + C + H) * 320 * 4
binary mean coordinate bytes   = N * (R + C) * 320 / 8
```

`R` and `C` include the retained contextual prefix/suffix vectors, not just
grid rows and columns. Binary coordinates are 32 times smaller than their float
counterparts, but the floats remain stored: quantization adds a copy, not a
32-fold reduction in total disk use. These are coordinate sizes, not allocation
sizes: alignment, multivector offsets, deletion maps, and segment metadata add
overhead.

For illustration, **100,000 pages with `R + C = 64` and `H = 300`** need about
**43.4 GiB of float coordinates on disk** and **244 MiB of binary coordinates in
RAM and on disk**. The upstream rough estimate for two `m=16` graphs is another
29 MiB, before extra payload-aware links and implementation overhead. This is
an example, not a measurement of the indexed corpus. Payloads, keyword indexes,
ID tracking, WAL, snapshots, and optimizer temporary segments also consume disk.
Changing memory tiers does not remove any of these disk costs.

At the default prefetch limit, each query can rerank up to 400 distinct candidate
pages per shard. With `H = 300`, that represents roughly 146 MiB of float
coordinates per query per shard if all 400 pages are scored, before page-read
amplification. Warm cache hits reduce physical I/O, not the scoring work.
Larger `top_k`, more shards, concurrent requests, and query expansion increase
the aggregate working set.

The pod's 30 GiB limit must accommodate pinned structures **plus** the OS page
cache, Qdrant runtime, ingestion, concurrent searches, and optimizer peaks.
`cold` data can still consume RAM through the page cache, and that cache counts
toward the container limit. Do not size pinned allocations to the full limit or
treat a small pinned index as evidence that the disk volume is large enough.
Use representative pooled-vector counts and mixed ingestion/search measurements
before resizing the existing resource requests or volume.

See Qdrant's [memory tiers](https://qdrant.tech/documentation/ops-configuration/memory-tiers/)
and [capacity planning](https://qdrant.tech/documentation/capacity-planning/) for
the placement semantics and approximate non-vector costs.

### Page identity and payloads

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
`indexed_with_file_hash`, `page_hash`, `effective_dpi`, and `processing_fingerprint`. The agent
reads this nested contract and deduplicates by document identity plus page number.

Initialization rejects incompatible vector names/dimensions. Compatible
collections have these memory tiers reconciled without deleting data or
unrelated payload indexes. Reconciliation preserves vector datatypes, HNSW
search/build parameters, and existing binary encoding settings; it does not add
or replace quantization on existing fields. Already-matching collection settings
are not patched again; omitted collection settings retain Qdrant's equivalent
defaults of cold payloads and pinned ID tracking.
Storage changes can trigger background optimization;
allow it to finish before assessing memory use or newly indexed search results.
Existing document IDs/payloads are also checked before reuse.

Writes are page-level, not an atomic document snapshot. In-process duplicate
events are serialized and source ETags are checked before completion, but
cross-replica updates to the same document are not a distributed transaction.
Route competing versions of a document to one owner if strict ordering is needed.
Already-written pages can be retrieved while other pages are still processing;
retrieval does not filter on the completed file hash.

## Queue reliability

Both single Event Grid objects and arrays are supported. Slots refill as soon
as documents finish; a slow document does not block a whole received batch.
Receive calls, document handling, settlement, and resource cleanup have deadlines.
Lock renewal uses the asynchronous SDK. Transient failures are abandoned for
retry, while invalid documents and exhausted token-limit recovery are preserved
in the dead-letter queue. A delivery-count guard parks repeatedly unsettled
messages before opening their document bytes.

The processor consumes one queue. Cancellation-resistant cleanup stops the listener
rather than reusing a slot whose interrupted work may still be active.

The consumer accepts PDF/JPEG/PNG events, but the default infrastructure event
subscription only forwards `.pdf` paths in the `documents` container. To ingest
images, explicitly configure an appropriate event filter or send valid
BlobCreated messages to the configured queue.

## Configuration

Connection settings are `SERVICE_BUS_NAMESPACE_NAME`, `SERVICE_BUS_QUEUE_NAME`,
`DATA_STORAGE_ACCOUNT_NAME`, `COLPALI_INFERENCE_ENDPOINT`, `QDRANT_ENDPOINT`,
`QDRANT_API_KEY`, and the managed-identity configuration.
Azure credentials honor `AZURE_TOKEN_CREDENTIALS`; Helm selects
`WorkloadIdentityCredential` for the projected AKS token.

| Setting | Default | Purpose |
|---|---|---|
| `QDRANT_COLLECTION_NAME` | `colpali-documents` | Explicit collection, shared with retrieval |
| `COLPALI_EMBEDDING_DIMENSION` | 320 | Must match the configured inference model |
| `MODEL_ID` | `TomoroAI/tomoro-ai-colqwen3-embed-4b-awq` | Fingerprint model identity |
| `INDEXING_REVISION` | `1` | Invalidate caches after inference/weight changes |
| `COLPALI_STORAGE_DPI` | 500 | Stored PDF resolution |
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

Use Python 3.14 and the committed lockfile. From the repository root:

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
docker run --rm -d --name colpali-test-qdrant -p 127.0.0.1:46333:6333 -e QDRANT__TELEMETRY_DISABLED=true qdrant/qdrant:v1.20.0
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
against Qdrant 1.20.0, matching the processor client and Helm server versions.
Server-backed scenarios also verify persisted memory placement for new and
populated legacy collections, preservation of pages and search settings, and
idempotent reinitialization.

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
