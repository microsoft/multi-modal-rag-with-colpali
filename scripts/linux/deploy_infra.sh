#!/bin/bash
# Complete deployment script: deploys Bicep infrastructure
# Usage: ./deploy_infra.sh [--resource-group <resource-group>] [--deploy-roles <true|false>]
# Note: AKS and Event Grid always deploy (containers pushed later via Helm)
# Note: baseName and location are defined in infra/src/main.bicepparam

set -euo pipefail

# Default values
RESOURCE_GROUP="colpali-rg"
DEPLOY_ROLES="true"

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --resource-group)
            RESOURCE_GROUP="$2"
            shift 2
            ;;
        --deploy-roles)
            DEPLOY_ROLES="$2"
            shift 2
            ;;
        -h|--help)
            echo "Usage: $0 [--resource-group <resource-group>] [--deploy-roles <true|false>]"
            echo "  --resource-group: Resource group name (default: colpali-rg)"
            echo "  --deploy-roles: Deploy role assignments (default: true)"
            exit 0
            ;;
        *)
            echo "Unknown parameter: $1"
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

# Check if online endpoint already exists
echo "Checking if online endpoint already exists..."
BICEP_PARAM_CONTENT=$(cat "$PROJECT_ROOT/infra/src/main.bicepparam")
BASE_NAME=$(echo "$BICEP_PARAM_CONTENT" | grep -oP "param baseName = '\K[^']+")
if [ -z "$BASE_NAME" ]; then
    echo "Could not find baseName in main.bicepparam" >&2
    exit 1
fi

ENDPOINT_NAME="oep-$BASE_NAME"
WORKSPACE_NAME_FROM_PARAM="mlw-$BASE_NAME"

ENDPOINT_EXISTS=false
SUBSCRIPTION_ID=$(az account show --query id -o tsv)
RESOURCE_ID="/subscriptions/$SUBSCRIPTION_ID/resourceGroups/$RESOURCE_GROUP/providers/Microsoft.MachineLearningServices/workspaces/$WORKSPACE_NAME_FROM_PARAM/onlineEndpoints/$ENDPOINT_NAME"

if az resource show --ids "$RESOURCE_ID" >/dev/null 2>&1; then
    ENDPOINT_EXISTS=true
    echo "  Endpoint '$ENDPOINT_NAME' already exists - will skip creation to preserve traffic allocation"
else
    echo "  Endpoint '$ENDPOINT_NAME' does not exist - will create it"
fi

CREATE_ENDPOINT=$( [ "$ENDPOINT_EXISTS" = true ] && echo "false" || echo "true" )

# Use absolute paths for the Bicep files
BICEP_PARAM_FILE="$PROJECT_ROOT/infra/src/main.bicepparam"
echo "Using Bicep parameter file: $BICEP_PARAM_FILE"

# Deploy resources with Bicep using the parameter file
echo "Deploying resources with Bicep..."
echo "Resource Group: '$RESOURCE_GROUP'"
echo "Bicep Param File: '$BICEP_PARAM_FILE'"
echo "User Object ID: '$USER_OBJECT_ID'"
echo "Deploy Roles: '$DEPLOY_ROLES'"

DEPLOYMENT_OUTPUT=$(az deployment group create \
    --resource-group "$RESOURCE_GROUP" \
    --parameters "$BICEP_PARAM_FILE" \
    userObjectId="$USER_OBJECT_ID" \
    deployRoleAssignments="$DEPLOY_ROLES" \
    createOnlineEndpoint="$CREATE_ENDPOINT" \
    --query "properties.outputs" \
    -o json)

if [ $? -ne 0 ]; then
    echo "Bicep deployment failed" >&2
    exit 1
fi

# Convert all Bicep outputs to environment variables automatically
declare -A env_values
while IFS= read -r line; do
    if [[ $line =~ \"([^\"]+)\":[[:space:]]*\{[[:space:]]*\"value\":[[:space:]]*\"([^\"]*)\" ]]; then
        output_name="${BASH_REMATCH[1]}"
        output_value="${BASH_REMATCH[2]}"

        # Convert camelCase to UPPER_SNAKE_CASE for env var naming
        env_var_name=$(echo "$output_name" | sed 's/\([a-z]\)\([A-Z]\)/\1_\2/g' | tr '[:lower:]' '[:upper:]')

        env_values["$env_var_name"]="$output_value"
    fi
done <<< "$DEPLOYMENT_OUTPUT"

# Preserve all existing environment variables from .env file
ENV_FILE="$PROJECT_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    echo "Preserving existing environment variables from .env file..."
    preserved_count=0
    while IFS= read -r line; do
        if [[ $line =~ ^([^#][^=]*?)=(.*)$ ]]; then
            key="${BASH_REMATCH[1]}"
            value="${BASH_REMATCH[2]}"
            # Only preserve if not already set by Bicep outputs (Bicep takes precedence)
            if [ -z "${env_values[$key]:-}" ]; then
                env_values["$key"]="$value"
                ((preserved_count++))
            fi
        fi
    done < "$ENV_FILE"

    if [ $preserved_count -gt 0 ]; then
        echo "  Preserved $preserved_count existing environment variables"
    fi
fi

# Create .env file in project root - automatically generate from env_values array
echo "Creating .env file at $ENV_FILE"

# Sort keys and write to file
{
    for key in $(printf '%s\n' "${!env_values[@]}" | sort); do
        value="${env_values[$key]}"
        if [ -n "$value" ]; then
            echo "$key=$value"
        fi
    done
} > "$ENV_FILE"

echo ""
echo "Infrastructure deployment complete!"
