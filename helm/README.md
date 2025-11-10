# Helm Charts

This directory contains Helm charts for deploying the ColQwen2 multi-modal RAG solution to Kubernetes.

All application components are deployed to the `colpali-stack` namespace for resource isolation and management. The Helm charts handle the deployment of:

- ColQwen2 inference service
- Document processor service
- Qdrant vector database
- Supporting configurations and services

For individual service details and containerized applications, see the `/modules` directory.
