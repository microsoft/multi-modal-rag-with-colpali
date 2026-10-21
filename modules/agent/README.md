# Agent Demo

A containerized chat interface for document Q&A using Azure OpenAI and ColPali-based retrieval.

> [!IMPORTANT]
>
> This is a **demo** built with Chainlit to show what ColPali can do. Not production-ready.

For local development, set `AZURE_TOKEN_CREDENTIALS=dev` before starting the agent.
The Helm deployment configures `AZURE_TOKEN_CREDENTIALS=WorkloadIdentityCredential`.

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
| Prefetch | `mean_column_pooled`, `mean_row_pooled` | Binary quantization, 2x oversampling | Fast approximate candidate retrieval |
| Rerank | `hierarchical_pooled` | Exact search | Accurate final scoring |

## Agent Flow

1. User submits query via Agent UI
2. Generate related search queries (query expansion)
3. Retrieve relevant pages using ColPali + Qdrant
4. Generate answer with Azure OpenAI using retrieved context
5. Return response with source citations

## Implemented Retrieval Defaults

Single-query and query-expanded retrieval return at most **5 unique pages** by
default. Each query prefetches **200 candidates per mean-pooled vector**, using
HNSW `ef=64`, `exact=false`, and `indexed_only=true`. Binary prefetch rescoring is
enabled with **2x oversampling** against the full-precision mean vectors on disk.
The candidate union is then scored using `hierarchical_pooled` (pooling factor
3, no HNSW index). A smaller requested `top_k` does not shrink the candidate
pool; requests above 200 expand each prefetch to at least `top_k`.

The chart pins Qdrant **1.17.1**; newly created mean-vector indexes explicitly use
HNSW `m=16` and `ef_construct=100`. Full-precision vectors remain on disk, with
binary mean-vector copies in RAM. `indexed_only` can exclude unoptimized
segments: allow indexing/optimization to catch up when validating newly
ingested data, and check server behavior for small collections. This is not
document-completion filtering or atomic publication; individual pages can
become searchable before a whole document is complete.

## API Error Responses

Internal failures return a generic message; exception details and tracebacks are
logged server-side, not sent to clients. The agent's `run_stream` error events
also use this generic message, so exception details never enter the event payload
passed to API consumers.

- `/chat` and unhandled API failures return HTTP 500 with `error` and `detail` fields.
- `/chat/stream` keeps the SSE response and emits a terminal `error` event with
  generic `content` and a `timestamp`, including when the agent yields an error event.

## API Tests

Run `uv run --frozen pytest tests` from `modules/agent`. The API tests replace the
agent and do not require Azure credentials or running backend services.
