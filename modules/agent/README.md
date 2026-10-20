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
| Prefetch | `mean_pooled_columns`, `mean_pooled_rows` | Binary quantization, 2x oversampling | Fast approximate candidate retrieval |
| Rerank | `hierarchical_pooled` | Unquantized MaxSim over the shortlist | Accurate final scoring |

Query token vectors are not pooled. The final MaxSim scores are exact for the
stored hierarchical-pooled vectors in the shortlist, not for every original
page vector in the corpus; the HNSW prefetch remains approximate.

### Search settings and PULSAR

The current retriever sets `prefetch_limit = top_k * 10` for each row/column
prefetch and `hnsw_ef = 200`, with binary quantization, `rescore=False`, and
`oversampling=2.0`. These are implementation settings, not separately exposed
environment variables. `search_documents_batch` defaults to `top_k=10`;
the single-query `search_documents` wrapper defaults to `top_k=5`.

[Section 5.2 of PULSAR](https://arxiv.org/html/2608.28572#S5.SS2) instead reports a
production prefetch limit of **200** and HNSW ef of **64**, following sweeps on
ViDoRe V3; its pooling comparison used ef **128**. Those settings and the paper's
production `top_k=5` are reference operating points, not this agent's defaults
or universally optimal values. Evaluate candidate recall and search latency on
your corpus before changing them. Query expansion, embedding generation, image
fetches, and answer generation also contribute to this agent's end-to-end
latency; the paper's vector-search timings do not include all of those steps.

## Agent Flow

1. User submits query via Agent UI
2. Generate related search queries (query expansion)
3. Retrieve relevant pages using ColPali + Qdrant
4. Generate answer with Azure OpenAI using retrieved context
5. Return response with source citations

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
