# Agent Demo

This module provides a containerized agent demo for intelligent document Q&A using Azure OpenAI and ColQwen-based document retrieval.

> [!IMPORTANT]
>
> - This implementation is a lightweight **demo** with Chainlit to demonstrate the power of Colpali. It is not intended for production use.

## Architecture Overview

This service uses a **distributed architecture** for separation of concerns and flexible scaling:

1. **Agent API** – FastAPI backend exposing chat and health endpoints
2. **Agent UI** – Chainlit-based chat interface for interactive document Q&A
3. **Azure OpenAI** – Hosts the chat model used to reason over user questions and retrieved content
4. **ColQwen + Qdrant** – Visual/text document retrieval stack providing context for answers

## Key Components

- `src/app.py` – FastAPI REST API with chat and health endpoints
- `src/chainlit_ui.py` – Chainlit UI for interactive chat
- `src/agent.py` – Agent orchestration, query expansion, and document retrieval

## Agent Flow

```yaml
Agent Request Flow:
  User Query: Chat message from Agent UI
  Query Expansion: Generate related search queries
  Document Retrieval: ColQwen/Qdrant-based multimodal search
  Answer Generation: Azure OpenAI chat model over retrieved content
  Source Citations: Returned alongside the final answer
```
