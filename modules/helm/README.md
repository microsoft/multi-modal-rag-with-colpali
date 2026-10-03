# Helm Charts

This directory contains Helm charts for deploying the ColPali/ColQwen multi-modal RAG solution to Kubernetes.

All application components are deployed to the `colpali-stack` namespace for resource isolation and management. The Helm charts handle the deployment of:

- ColQwen3 inference service (`TomoroAI/tomoro-colqwen3-embed-4b`, vLLM GPU sidecar + CPU pooling shim)
- Document processor service
- Qdrant vector database
- Agent API service (RAG backend)
- Agent UI service (Chainlit web interface)
- Supporting configurations and services

For individual service details and containerized applications, see the `/modules` directory.
