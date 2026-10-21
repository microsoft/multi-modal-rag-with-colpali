# Agent Demo

A containerized chat interface for document Q&A using Azure OpenAI and ColPali-based retrieval.

> [!IMPORTANT]
>
> This is a **demo** built with Chainlit to show what ColPali can do. Not production-ready.

For local development, set `AZURE_TOKEN_CREDENTIALS=dev` before starting the agent.
The Helm deployment configures `AZURE_TOKEN_CREDENTIALS=WorkloadIdentityCredential`.

## Dependency compatibility checks

Use Python 3.14 and the committed lockfile:

```shell
uv sync --locked --no-editable --no-dev --group test
uv run --locked --no-dev --group test python -m pytest tests -q
```

These offline-service tests cover API startup/shutdown, chat and SSE responses,
request validation, CORS, OpenTelemetry middleware, the Chainlit UI and Engine.IO
handshake, MSAL assertion signing, and the real agent's framework client.
Telemetry setup is patched before importing the API, including when an
Application Insights connection string is configured. They do not call Azure or
the inference service. Prerelease handling is configured in `pyproject.toml` for
both local and container installs. Transitive security floors are recorded in
`tool.uv.constraint-dependencies`.

Only `agent-framework-core` is required for the chat types and
`AzureOpenAIChatClient`. It is pinned to the previously resolved core version;
the umbrella package and its unused integrations (including Mem0) are not installed.

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

[Section 5.2 of PULSAR](https://arxiv.org/html/2608.28572#S5.SS2) reports a
production prefetch limit of **200** and HNSW ef of **64**, following sweeps on
ViDoRe V3; its pooling comparison used ef **128** and production returns the top
**5** pages. Query expansion, embedding generation, image
fetches, and answer generation also contribute to this agent's end-to-end
latency; the paper's vector-search timings do not include all of those steps.

### Pending indexer-port integration

The open [indexer PR #72](https://github.com/microsoft/multi-modal-rag-with-colpali/pull/72)
adopts those production operating points. Both single-query and expanded
query paths default to 5 unique pages, with `max(200, top_k)` candidates per
mean-vector prefetch, `hnsw_ef=64`, `exact=False`, `indexed_only=True`, and binary
rescoring enabled with 2x oversampling.

That port changes the mean-vector names to `mean_row_pooled` and
`mean_column_pooled` and reads nested source metadata, including the stored
page-image URL. Citations are deduplicated by document identity and page number.

`indexed_only` selects optimized Qdrant segments rather than filtering on a
completed document hash. Individual pages become searchable during processing.

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
