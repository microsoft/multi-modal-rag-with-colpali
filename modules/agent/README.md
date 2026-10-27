# Agent Demo

A containerized chat interface for document Q&A using Azure OpenAI and ColPali-based retrieval.

> [!IMPORTANT]
>
> This is a **demo** built with Chainlit to show what ColPali can do. Not production-ready.

For local development, set `AZURE_TOKEN_CREDENTIALS=dev` before starting the agent.
The Helm deployment configures `AZURE_TOKEN_CREDENTIALS=WorkloadIdentityCredential`.

## Development

From `modules/agent`, use Python 3.14 and the committed lockfile:

```shell
uv sync --locked --no-editable --no-dev --group test
uv run --locked --no-dev --group test python -m pytest tests -q
```

These offline-service tests cover API startup/shutdown, chat and SSE responses,
request validation, CORS, OpenTelemetry middleware, the Chainlit UI and Engine.IO
handshake, MSAL assertion signing, and the real agent's framework client.
They do not call Azure or the inference service.

The UI runs as `uvicorn agent.ui_app:app --loop asyncio`, using Chainlit's ASGI
mounting API. Avoid the Chainlit CLI on Python 3.14: its `nest_asyncio` loop patch
breaks async-library detection during static asset requests and can leave the
browser blank even when `/` returns HTTP 200.

## Architecture Overview

The agent uses these services:

1. **Agent API** – FastAPI backend exposing chat and health endpoints
2. **Agent UI** – Chainlit-based chat interface for interactive document Q&A
3. **Azure OpenAI** – Hosts the chat model used to reason over user questions and retrieved content
4. **ColPali + Qdrant** – Visual/text document retrieval stack providing context for answers

## Retrieval Strategy

Uses the **mean_pooling_with_hierarchical_quantized_prefetch_only** strategy:

| Stage | Vector | Params | Purpose |
|-------|--------|--------|--------|
| Prefetch | `mean_column_pooled`, `mean_row_pooled` | Binary quantization, `rescore=false` | Fast approximate candidate retrieval |
| Rerank | `hierarchical_pooled` | MaxSim over the candidate union, HNSW disabled | Final scoring with pooled token vectors |

## Agent Flow

1. User submits query via Agent UI
2. Generate related search queries (query expansion)
3. Retrieve relevant pages using ColPali + Qdrant
4. Generate answer with Azure OpenAI using retrieved context
5. Return response with source citations

`/chat` returns the complete answer and cited pages in `sources`, including page
images when available. `/chat/stream` emits those same pages as `source_delta`
events before its terminal `done` event.

## Implemented Retrieval Defaults

Single-query and query-expanded retrieval return at most **5 unique pages** by
default. Each query prefetches **200 candidates per mean-pooled vector**, using
HNSW `ef=64`, `exact=false`, and `indexed_only=true`. Binary prefetch uses
`rescore=false`: candidates are not rescored against float mean vectors.
The candidate union (up to 400 pages per query per shard at the default limit)
is then scored using `hierarchical_pooled` (pooling factor 3, no HNSW index).
A smaller requested `top_k` does not shrink the candidate
pool; requests above 200 expand each prefetch to at least `top_k`.
Across expanded queries, each page retains its highest returned score before
the final top-k selection. Query embedding batches must match the requested
query count; mismatches are logged and rejected before any Qdrant search.

The chart pins Qdrant **1.20.0**; newly created mean-vector indexes explicitly use
HNSW `m=16` and `ef_construct=100`. Full-precision vectors remain on disk, with
binary mean-vector copies and their HNSW graphs pinned in RAM. Original vectors
and payloads use the `cold` memory tier; the ID tracker and managed keyword
indexes remain pinned. Float mean-vector copies remain stored, but are not read
for prefetch rescoring; final reranking uses the float hierarchical vectors.
This keeps the optimized candidate-scoring path resident, not the whole
collection: cold data still enters the OS page cache, and ingestion/optimization
still need float copies. See the [RAM and disk accounting](../document_processor/README.md#ram-disk-and-query-working-set).
Placement is reconciled by the processor without changing
the retrieval settings above. `indexed_only` can exclude unoptimized
segments: allow indexing/optimization to catch up when validating newly
ingested data, and check server behavior for small collections. This is not
document-completion filtering or atomic publication; individual pages can
become searchable before a whole document is complete.

The retriever reads nested `source` and `metadata` payloads, fetches the stored
page image from `source.page_image_url`, and deduplicates citations using document
identity plus page number. `QDRANT_COLLECTION_NAME` must match the indexer's
collection, and query inference must use the model that produced those vectors.

The search settings above are implementation defaults in
`src/agent/document_retriever.py`; they are not all Helm/environment overrides.
For `top_k=5`, the candidate union contains up to 400 distinct pages before
reranking returns the best five. A candidate is a page/Qdrant
point, not an individual token vector or an entire document. Each prefetch uses
`max(200, top_k)`, so it grows only when more than 200 final results are requested.
The available deployment controls are listed in the
[Helm tuning guide](../helm/colpali-stack/README.md#indexing-and-inference-tuning).

## API Error Responses

Internal failures return a generic message; exception details and tracebacks are
logged server-side, not sent to clients. The agent's `run_stream` error events
also use this generic message, so exception details never enter the event payload
passed to API consumers.

- `/chat` and unhandled API failures return HTTP 500 with `error` and `detail` fields.
- `/chat/stream` keeps the SSE response and emits a terminal `error` event with
  generic `content` and a `timestamp`, including when the agent yields an error event.
