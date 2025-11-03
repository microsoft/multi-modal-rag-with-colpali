# Modules

Core components for the ColPali multi-modal RAG solution. Each module handles a specific part of the document processing and retrieval pipeline.

| Module | Purpose | Description |
|--------|---------|-------------|
| **colpali** | Model Deployment | Deploys ColPali models to Azure ML online endpoints for GPU-accelerated inference |
| **indexer** | Document Processing | Function App that processes PDFs and generates ColPali embeddings via the online endpoint |
| **index** | Search Setup | Creates and configures the Azure AI Search index for storing document embeddings |
| **retrieval** | Query Interface | Retrieval agent built on top of AI Foundry Agent Service |
