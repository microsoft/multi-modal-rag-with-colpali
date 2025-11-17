# Modules

Core components for the ColQwen2 multi-modal RAG solution. Each module handles a specific part of the document processing and retrieval pipeline, designed for containerized deployment on Kubernetes.

| Module | Purpose | Description |
|--------|---------|-------------|
| **agent** | RAG Agent | Chainlit-based conversational interface for multi-modal document Q&A with ColQwen2 integration |
| **colqwen_inference** | Model Inference | Containerized ColQwen2 visual document understanding model for Kubernetes inference serving with FastAPI |
| **document_processor** | Document Processing | Service Bus-driven document processor that converts PDFs to images, creates ColQwen2 embeddings, and indexes in QDRANT vector database |
| **helm** | Kubernetes Deployment | Helm charts for deploying the complete ColPali stack to AKS clusters |
| **infra** | Infrastructure | Bicep templates for provisioning Azure resources (AKS, Storage, Event Grid, Service Bus, etc.) |
