#!/bin/bash
# Complete deployment script: deploys Bicep infrastructure
# Usage: ./deploy_infra.sh [-g resource-group] [-r deploy-roles] [-c deploy-container-apps]
# Note: baseName and location are defined in infra/src/main.bicepparam

set -e  # Exit on any error

# Default values
RESOURCE_GROUP="colqwen-rg"
DEPLOY_ROLES="true"
DEPLOY_CONTAINER_APPS="false"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -g|--resource-group)
            RESOURCE_GROUP="$2"
            shift 2
            ;;
        -r|--deploy-roles)
            DEPLOY_ROLES="$2"
            shift 2
            ;;
        -c|--deploy-container-apps)
            DEPLOY_CONTAINER_APPS="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [-g resource-group] [-r deploy-roles] [-c deploy-container-apps]"
            echo "  -g, --resource-group       Resource group name (default: colqwen-rg)"
            echo "  -r, --deploy-roles         Deploy role assignments (default: true)"
            echo "  -c, --deploy-container-apps Deploy container apps and Event Grid (default: false)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Get the absolute path of the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Get the absolute path of the project root (go up two levels: scripts/linux -> scripts -> root)
PROJECT_ROOT="$(dirname "$(dirname "$SCRIPT_DIR")")"
# Change to the project root directory
cd "$PROJECT_ROOT"

echo "Project root: $PROJECT_ROOT"

# Get user object ID
echo "Getting user object ID..."
USER_OBJECT_ID=$(az ad signed-in-user show --query id -o tsv)
if [ $? -ne 0 ]; then
    echo "Failed to get user object ID" >&2
    exit 1
fi

# Use absolute paths for the Bicep files
BICEP_PARAM_FILE="$PROJECT_ROOT/infra/src/main.bicepparam"
echo "Using Bicep parameter file: $BICEP_PARAM_FILE"

# Deploy resources with Bicep using the parameter file
echo "Deploying resources with Bicep..."
echo "Resource Group: '$RESOURCE_GROUP'"
echo "Bicep Param File: '$BICEP_PARAM_FILE'"
echo "User Object ID: '$USER_OBJECT_ID'"
echo "Deploy Roles: '$DEPLOY_ROLES'"
echo "Deploy Container Apps: '$DEPLOY_CONTAINER_APPS'"

DEPLOYMENT_OUTPUT=$(az deployment group create \
    --resource-group "$RESOURCE_GROUP" \
    --parameters "$BICEP_PARAM_FILE" userObjectId="$USER_OBJECT_ID" deployRoleAssignments="$DEPLOY_ROLES" deployContainerApps="$DEPLOY_CONTAINER_APPS" \
    --query properties.outputs -o json)

if [ $? -ne 0 ]; then
    echo "Bicep deployment failed" >&2
    exit 1
fi

# Parse deployment outputs using jq
WORKSPACE_NAME=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.amlWorkspaceName.value')
COMPUTE_CLUSTER_NAME=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.amlComputeClusterName.value')
EMBEDDING_ENDPOINT_NAME=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.amlEmbeddingEndpointName.value')
EMBEDDING_ENDPOINT_URL=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.amlEmbeddingEndpointUrl.value')
ACR_NAME=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.acrName.value')
ACR_LOGIN_SERVER=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.acrLoginServer.value')
AI_SEARCH_SERVICE_URL=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.aiSearchServiceUrl.value')
AI_SEARCH_SERVICE_NAME=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.aiSearchServiceName.value')
AI_SEARCH_INDEX_NAME=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.aiSearchIndexName.value')
EMBEDDING_ENDPOINT_TYPE=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.amlEmbeddingEndpointType.value')
EMBEDDING_ENDPOINT_COUNT=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.amlEmbeddingEndpointCount.value')

# Container Apps outputs (only available when DEPLOY_CONTAINER_APPS = true)
if [ "$DEPLOY_CONTAINER_APPS" = "true" ]; then
    QDRANT_ENDPOINT=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.qdrantEndpoint.value')
    DOC_PROCESSOR_ENDPOINT=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.docProcessorEndpoint.value')
    CONTAINER_APPS_ENVIRONMENT=$(echo "$DEPLOYMENT_OUTPUT" | jq -r '.containerAppsEnvironmentName.value')
else
    QDRANT_ENDPOINT=""
    DOC_PROCESSOR_ENDPOINT=""
    CONTAINER_APPS_ENVIRONMENT=""
fi

echo "Deployment outputs:"
echo "  AML Workspace: $WORKSPACE_NAME"
echo "  AML Compute Cluster: $COMPUTE_CLUSTER_NAME"
echo "  Embedding Endpoint Name: $EMBEDDING_ENDPOINT_NAME"
echo "  Embedding Endpoint URL: $EMBEDDING_ENDPOINT_URL"
echo "  ACR Name: $ACR_NAME"
echo "  ACR Login Server: $ACR_LOGIN_SERVER"
echo "  AI Search Service Name: $AI_SEARCH_SERVICE_NAME"
echo "  AI Search URL: $AI_SEARCH_SERVICE_URL"
echo "  AI Search Index Name: $AI_SEARCH_INDEX_NAME"
echo "  Embedding Endpoint Type: $EMBEDDING_ENDPOINT_TYPE"
echo "  Embedding Endpoint Count: $EMBEDDING_ENDPOINT_COUNT"

if [ "$DEPLOY_CONTAINER_APPS" = "true" ]; then
    echo "  QDRANT Endpoint: $QDRANT_ENDPOINT"
    echo "  Doc Processor Endpoint: $DOC_PROCESSOR_ENDPOINT"
    echo "  Container Apps Environment: $CONTAINER_APPS_ENVIRONMENT"
else
    echo "  Container Apps: Not deployed (use -c true to deploy)"
fi

# Get subscription ID
echo "Getting subscription ID..."
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
if [ $? -ne 0 ]; then
    echo "Failed to get subscription ID" >&2
    exit 1
fi

# Get ACR credentials
echo "Retrieving ACR credentials..."
ACR_CREDENTIALS=$(az acr credential show --name "$ACR_NAME" --query "{username:username, password:passwords[0].value}" -o json 2>/dev/null)
if [ $? -ne 0 ]; then
    echo "Warning: Failed to retrieve ACR credentials. They will not be added to .env file."
    ACR_USERNAME=""
    ACR_PASSWORD=""
else
    ACR_USERNAME=$(echo "$ACR_CREDENTIALS" | jq -r '.username')
    ACR_PASSWORD=$(echo "$ACR_CREDENTIALS" | jq -r '.password')
fi

# Create .env file in project root
ENV_FILE="$PROJECT_ROOT/.env"
echo "Creating .env file at $ENV_FILE"

cat > "$ENV_FILE" << EOF
RESOURCE_GROUP=$RESOURCE_GROUP
SUBSCRIPTION_ID=$SUBSCRIPTION_ID
AML_WORKSPACE_NAME=$WORKSPACE_NAME
AML_COMPUTE_NAME=$COMPUTE_CLUSTER_NAME
AML_EMBEDDING_ENDPOINT_NAME=$EMBEDDING_ENDPOINT_NAME
AML_EMBEDDING_ENDPOINT_URL=$EMBEDDING_ENDPOINT_URL
AML_EMBEDDING_ENDPOINT_TYPE=$EMBEDDING_ENDPOINT_TYPE
AML_EMBEDDING_ENDPOINT_COUNT=$EMBEDDING_ENDPOINT_COUNT
ACR_NAME=$ACR_NAME
ACR_LOGIN_SERVER=$ACR_LOGIN_SERVER
ACR_USERNAME=$ACR_USERNAME
ACR_PASSWORD=$ACR_PASSWORD
AI_SEARCH_ENDPOINT=$AI_SEARCH_SERVICE_URL
AI_SEARCH_SERVICE_NAME=$AI_SEARCH_SERVICE_NAME
AI_SEARCH_INDEX_NAME=$AI_SEARCH_INDEX_NAME
QDRANT_ENDPOINT=$QDRANT_ENDPOINT
QDRANT_COLLECTION_NAME=colpali-documents
DOC_PROCESSOR_ENDPOINT=$DOC_PROCESSOR_ENDPOINT
CONTAINER_APPS_ENVIRONMENT=$CONTAINER_APPS_ENVIRONMENT
EOF

echo "Deployment complete."
echo ".env file created with deployment outputs."
