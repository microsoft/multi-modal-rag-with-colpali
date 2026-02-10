# Modules

Core components for the ColPali/ColQwen multi-modal RAG solution. Each module handles a specific part of the document processing and retrieval pipeline, designed for containerized deployment on Kubernetes.

| Module | Purpose | Description |
|--------|---------|-------------|
| **agent** | RAG Agent | Chainlit-based conversational interface for multi-modal document Q&A with ColPali/ColQwen integration |
| **colpali_inference** | Model Inference | Containerized ColPali visual document understanding model for Kubernetes inference serving with FastAPI |
| **document_processor** | Document Processing | Service Bus-driven document processor that converts PDFs to images, creates ColPali/ColQwen embeddings, stores page images in Azure Blob Storage, and indexes vectors in Qdrant |
| **helm** | Kubernetes Deployment | Helm charts for deploying the complete ColPali stack to AKS clusters |
| **infra** | Infrastructure | Bicep templates for provisioning Azure resources (AKS, Storage, Event Grid, Service Bus, etc.) |
