# Agent Demo

A containerized chat interface for document Q&A using Azure OpenAI and ColPali-based retrieval.

> [!IMPORTANT]
>
> This is a **demo** built with Chainlit to show what ColPali can do. Not production-ready.

## Architecture Overview

This service uses a **distributed architecture** for separation of concerns and flexible scaling:

1. **Agent API** – FastAPI backend exposing chat and health endpoints
2. **Agent UI** – Chainlit-based chat interface for interactive document Q&A
3. **Azure OpenAI** – Hosts the chat model used to reason over user questions and retrieved content
4. **ColPali + Qdrant** – Visual/text document retrieval stack providing context for answers

## Retrieval Strategy

Uses the **mean_pooling_with_hierarchical_quantized_prefetch_only** strategy (optimal based on benchmarking):

| Stage | Vector | Params | Purpose |
|-------|--------|--------|--------|
| Prefetch | `mean_pooled_columns`, `mean_pooled_rows` | Binary quantization, 2x oversampling | Fast approximate candidate retrieval |
| Rerank | `hierarchical_pooled` | Exact search | Accurate final scoring |

## Agent Flow

1. User submits query via Agent UI
2. Generate related search queries (query expansion)
3. Retrieve relevant pages using ColPali + Qdrant
4. Generate answer with Azure OpenAI using retrieved context
5. Return response with source citations
