#!/bin/bash

# ColPali Pure Kubernetes Deployment Script
#
# Deploys the complete ColPali stack to AKS using pure Kubernetes approach:
# - ColQwen model download Job with shared storage
# - ColQwen inference Deployment with auto-scaling
# - Document processor service
# - Qdrant vector database
# - Ingress controller for external access
#
# Prerequisites: Infrastructure must be deployed first via deploy_infra.sh
# Reads configuration from .env file created by infrastructure deployment.

set -e

# Setup paths
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
cd "$PROJECT_ROOT"

echo "ColPali Kubernetes Deployment Starting"

# Load configuration from .env file
ENV_FILE="$PROJECT_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
    echo "ERROR: .env file not found. Infrastructure deployment required first."
    echo "ERROR: Run deploy_infra.sh to create infrastructure and configuration."
    exit 1
fi

echo "Loading configuration from .env file"
# Parse .env file
while IFS='=' read -r key value; do
    # Skip comments and empty lines
    if [[ $key =~ ^[[:space:]]*# ]] || [[ -z $key ]]; then
        continue
    fi
    # Remove any trailing whitespace and export
    key=$(echo "$key" | xargs)
    value=$(echo "$value" | xargs)
    declare "$key"="$value"
done < "$ENV_FILE"

# Validate required configuration
REQUIRED_VARS=(
    "RESOURCE_GROUP"
    "AKS_CLUSTER_NAME"
    "ACR_LOGIN_SERVER"
    "WORKLOAD_IDENTITY_CLIENT_ID"
    "AKS_KUBELET_IDENTITY_CLIENT_ID"
    "DATA_STORAGE_ACCOUNT_NAME"
    "SERVICE_BUS_NAMESPACE_NAME"
    "SERVICE_BUS_QUEUE_NAME"
    "KEY_VAULT_NAME"
    "QDRANT_API_KEY_SECRET_NAME"
    "QDRANT_READ_ONLY_API_KEY_SECRET_NAME"
    "APPLICATION_INSIGHTS_CONNECTION_STRING_SECRET_NAME"
)

for var in "${REQUIRED_VARS[@]}"; do
    if [[ -z "${!var}" ]]; then
        echo "ERROR: Missing required configuration: $var"
        echo "ERROR: Ensure infrastructure deployment completed successfully."
        exit 1
    fi
done

echo "Configuration loaded successfully"
echo "Resource Group: $RESOURCE_GROUP"
echo "AKS Cluster: $AKS_CLUSTER_NAME"

# Connect to AKS cluster
echo "Connecting to AKS cluster"
az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER_NAME" --overwrite-existing

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to get AKS credentials"
    exit 1
fi

# Verify cluster connectivity
echo "Verifying cluster connectivity"
kubectl cluster-info --request-timeout=10s > /dev/null
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to connect to AKS cluster"
    echo "ERROR: Check your credentials and network connection"
    exit 1
fi

echo "Successfully connected to AKS cluster"

# Update Helm dependencies
echo "Updating Helm chart dependencies"
cd "helm/colpali-stack"
helm dependency update
if [ $? -ne 0 ]; then
    echo "ERROR: Failed to update Helm dependencies"
    exit 1
fi
echo "Helm dependencies updated successfully"
cd "$PROJECT_ROOT"

# Deploy ColPali stack
echo "Deploying ColPali stack to Kubernetes"
echo "This may take several minutes..."

# Get image tags from .env
DOC_PROCESSOR_IMAGE_TAG="${DOCUMENT_PROCESSOR_IMAGE_TAG:-latest}"
COLQWEN_IMAGE_TAG="${COLQWEN_IMAGE_TAG:-latest}"

# Deploy ColPali stack with pure Kubernetes approach
helm upgrade --install colpali-stack "./helm/colpali-stack" \
    --namespace colpali-stack \
    --create-namespace \
    --set acrServer="$ACR_LOGIN_SERVER" \
    --set aksIdentityClientId="$WORKLOAD_IDENTITY_CLIENT_ID" \
    --set aksKubeletIdentityClientId="$AKS_KUBELET_IDENTITY_CLIENT_ID" \
    --set tenantId="$TENANT_ID" \
    --set storageAccountName="$DATA_STORAGE_ACCOUNT_NAME" \
    --set serviceBusNamespace="$SERVICE_BUS_NAMESPACE_NAME" \
    --set serviceBusQueueName="$SERVICE_BUS_QUEUE_NAME" \
    --set documentProcessor.imageTag="$DOC_PROCESSOR_IMAGE_TAG" \
    --set colqwenInference.imageTag="$COLQWEN_IMAGE_TAG" \
    --set keyVault.name="$KEY_VAULT_NAME" \
    --set keyVault.qdrantApiKeySecretName="$QDRANT_API_KEY_SECRET_NAME" \
    --set keyVault.qdrantReadOnlyApiKeySecretName="$QDRANT_READ_ONLY_API_KEY_SECRET_NAME" \
    --set keyVault.applicationInsightsConnectionStringSecretName="$APPLICATION_INSIGHTS_CONNECTION_STRING_SECRET_NAME" \
    --wait --timeout 20m

if [ $? -eq 0 ]; then
    echo "Deployment completed successfully"

    # Wait for services to stabilize
    sleep 5

    # Check deployment status
    echo "Checking deployment status"
    kubectl get pods
    kubectl get services
    kubectl get ingress

    # Get ingress controller service details
    INGRESS_IP=$(kubectl get svc colpali-stack-ingress-nginx-controller -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "")

    echo "ColPali Stack Deployment Summary"
    echo "Cluster: $AKS_CLUSTER_NAME"
    echo "Resource Group: $RESOURCE_GROUP"

    if [[ -n "$INGRESS_IP" ]]; then
        echo "LoadBalancer IP: $INGRESS_IP"
        echo "Qdrant Dashboard: http://$INGRESS_IP"
    else
        echo "LoadBalancer IP: Pending assignment"
        echo "Run 'kubectl get svc colpali-stack-ingress-nginx-controller' to check LoadBalancer status"
        echo "Access will be available at http://<EXTERNAL-IP>/qdrant/ when ready"
    fi
    echo "Authentication: Azure Workload Identity"

    # Check ColPali stack component status
    echo ""
    echo "Checking ColPali stack deployment status..."

    # Check document processor pods
    DOC_PROCESSOR_STATUS=$(kubectl get pods -l app=colpali-stack-document-processor --no-headers 2>/dev/null || echo "")
    if [[ "$DOC_PROCESSOR_STATUS" == *"Running"* ]]; then
        echo -e "\033[32mDocument Processor: Running\033[0m"
    else
        echo -e "\033[33mDocument Processor: Starting...\033[0m"
    fi

    # Check ColQwen inference pods
    COLQWEN_STATUS=$(kubectl get pods -l app=colpali-stack-colqwen-inference --no-headers 2>/dev/null || echo "")
    if [[ "$COLQWEN_STATUS" == *"Running"* ]]; then
        echo -e "\033[32mColQwen Inference: Running\033[0m"
    else
        echo -e "\033[33mColQwen Inference: Starting...\033[0m"
    fi

    # Check Qdrant pods
    QDRANT_STATUS=$(kubectl get pods -l app.kubernetes.io/name=qdrant --no-headers 2>/dev/null || echo "")
    if [[ "$QDRANT_STATUS" == *"Running"* ]]; then
        echo -e "\033[32mQdrant Vector Database: Running\033[0m"
    else
        echo -e "\033[33mQdrant Vector Database: Starting...\033[0m"
    fi

    # Check model download job status
    MODEL_JOB_STATUS=$(kubectl get jobs -l app=colpali-stack-colqwen-model-download --no-headers 2>/dev/null || echo "")
    if [[ "$MODEL_JOB_STATUS" == *"1/1"* ]]; then
        echo -e "\033[32mModel Download Job: Completed\033[0m"
    else
        echo -e "\033[33mModel Download Job: In progress...\033[0m"
    fi

    echo ""
    echo -e "\033[32mPure Kubernetes ColPali deployment completed!\033[0m"

else
    echo "ERROR: Deployment failed"
    echo "ERROR: Check the error messages above for details"
    exit 1
fi
