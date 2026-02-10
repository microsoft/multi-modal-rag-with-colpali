# Helm Charts

This directory contains Helm charts for deploying the ColPali/ColQwen multi-modal RAG solution to Kubernetes.

All application components are deployed to the `colpali-stack` namespace for resource isolation and management. The Helm charts handle the deployment of:

- ColQwen2/ColQwen3-4B inference service
- Document processor service
- Qdrant vector database
- Agent API service (RAG backend)
- Agent UI service (Chainlit web interface)
- Supporting configurations and services

For individual service details and containerized applications, see the `/modules` directory.
