@description('Base name for all resources')
param baseName string

@description('The location for all resources')
param location string = resourceGroup().location

@description('Flag to control whether to deploy role assignments (set to false if they already exist)')
param deployRoleAssignments bool = false

@description('Flag to control whether to deploy container apps (set to false if container images are not pushed yet)')
param deployContainerApps bool = false
@description('The SKU name for the Azure Container Registry')
@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param acrSku string = 'Basic'

@description('Enable admin user for the container registry')
param acrAdminUserEnabled bool = true

@description('The SKU name for the Azure Machine Learning workspace')
@allowed([
  'Basic'
  'Enterprise'
])
param amlSku string = 'Basic'

@description('The instance type for embedding model deployment')
@allowed([
  'Standard_DS3_v2'
  'Standard_DS4_v2'
  'Standard_DS5_v2'
  'Standard_F4s_v2'
  'Standard_F8s_v2'
  'Standard_F16s_v2'
  'Standard_NC6s_v3'
  'Standard_NC12s_v3'
  'Standard_NC24s_v3'
  'Standard_NC24ads_A100_v4'
  'Standard_ND40rs_v2'
])
param amlEmbeddingEndpointType string = 'Standard_NC24ads_A100_v4'

@description('The number of instances for embedding model deployment')
@minValue(1)
@maxValue(5)
param amlEmbeddingEndpointCount int = 1

@description('The instance type for job model deployment')
@allowed([
  'Standard_DS3_v2'
  'Standard_DS4_v2'
  'Standard_DS5_v2'
  'Standard_F4s_v2'
  'Standard_F8s_v2'
  'Standard_F16s_v2'
  'Standard_NC6as_T4_v3'
  'Standard_NC12as_T4_v3'
  'Standard_NC16as_T4_v3'
  'Standard_NC24ads_A100_v4'
  'Standard_ND40rs_v2'
])
param jobInstanceType string = 'Standard_NC16as_T4_v3'

@description('The maximum number of instances for job model deployment')
@minValue(1)
@maxValue(5)
param jobInstanceCount int = 1

@description('The optional object ID of the user to assign to the compute instance (if empty, will be auto-assigned)')
param userObjectId string = ''

@description('Whether to create the online endpoint. Automatically determined by deployment scripts based on endpoint existence.')
param createOnlineEndpoint bool = true

var acrName = replace('cr${baseName}', '-', '')
var amlWorkspaceName = 'mlw-${baseName}'
var amlComputeClusterName = 'mlcc-${baseName}'
var aiFoundryName = replace('aif-${baseName}', '-', '')
var amlEmbeddingEndpointName = 'oep-${baseName}'

module acrModule 'modules/containerRegistry.bicep' = {
  name: 'acrDeployment'
  params: {
    acrName: acrName
    location: location
    acrSku: acrSku
    acrAdminUserEnabled: acrAdminUserEnabled
  }
}

module amlSupportingModule 'modules/amlSupporting.bicep' = {
  name: 'amlSupportingDeployment'
  params: {
    baseName: baseName
    location: location
  }
}

module dataStorageModule 'modules/dataStorage.bicep' = {
  name: 'dataStorageDeployment'
  params: {
    baseName: baseName
    location: location
  }
}

module amlWorkspace 'modules/aml.bicep' = {
  name: 'amlDeployment'
  params: {
    amlWorkspaceName: amlWorkspaceName
    location: location
    amlSku: amlSku
    storageAccountId: amlSupportingModule.outputs.amlStorageAccountId
    keyVaultId: amlSupportingModule.outputs.keyVaultId
    applicationInsightsId: amlSupportingModule.outputs.applicationInsightsId
    containerRegistryId: acrModule.outputs.acrId
    amlWorkspaceIdentityId: amlSupportingModule.outputs.userAssignedIdentityId
    amlWorkspacePrincipalId: amlSupportingModule.outputs.userAssignedIdentityPrincipalId
  }
}

module computeCluster 'modules/computeCluster.bicep' = {
  name: 'computeClusterDeployment'
  params: {
    workspaceName: amlWorkspace.outputs.amlWorkspaceName
    clusterName: amlComputeClusterName
    location: location
    vmSize: jobInstanceType
    minNodeCount: 0
    maxNodeCount: jobInstanceCount
    idleSecondsBeforeScaledown: 1800
  }
}

module amlEmbeddingEndpoint 'modules/onlineEndpoint.bicep' = {
  name: 'amlEmbeddingEndpointDeployment'
  params: {
    endpointName: amlEmbeddingEndpointName
    location: location
    amlWorkspaceId: amlWorkspace.outputs.amlWorkspaceId
    endpointDescription: 'ColQwen2 embedding endpoint for document understanding'
    createEndpoint: createOnlineEndpoint
    tags: {
      purpose: 'document-embedding'
      model: 'colqwen2'
    }
  }
}

module aiFoundryModule 'modules/aiFoundry.bicep' = {
  name: 'aiFoundryDeployment'
  params: {
    aiFoundryName: aiFoundryName
    location: location
  }
}

module containerAppsSupportingModule 'modules/containerAppsSupporting.bicep' = if (deployContainerApps) {
  name: 'containerAppsSupportingDeployment'
  params: {
    baseName: baseName
    location: location
    containerRegistryId: acrModule.outputs.acrId
  }
}

module containerAppsModule 'modules/containerApps.bicep' = if (deployContainerApps) {
  name: 'containerAppsDeployment'
  params: {
    baseName: baseName
    location: location
    storageSku: 'Premium_LRS'
    containerRegistryName: acrName
    colpaliEndpointUrl: amlEmbeddingEndpoint.outputs.scoringUri
    dataStorageAccountName: dataStorageModule.outputs.dataStorageAccountName
    containerAppsIdentityId: containerAppsSupportingModule!.outputs.containerAppsIdentityId
  }
}

module eventGridModule 'modules/eventGrid.bicep' = if (deployContainerApps) {
  name: 'eventGridDeployment'
  params: {
    baseName: baseName
    location: location
    dataStorageAccountId: dataStorageModule.outputs.dataStorageAccountId
    dataStorageAccountName: dataStorageModule.outputs.dataStorageAccountName
    containerAppWebhookUrl: 'https://${containerAppsModule!.outputs.docProcessorEndpoint}/api/webhook'
  }
}

// Assign role assignments
module roleAssignmentsModule 'modules/roleAssignments.bicep' = {
  name: 'roleAssignmentsDeployment'
  params: {
    amlStorageAccountId: amlSupportingModule.outputs.amlStorageAccountId
    dataStorageAccountId: dataStorageModule.outputs.dataStorageAccountId
    keyVaultId: amlSupportingModule.outputs.keyVaultId
    aiFoundryServiceId: aiFoundryModule.outputs.aiFoundryId
    containerRegistryId: acrModule.outputs.acrId
    applicationInsightsId: amlSupportingModule.outputs.applicationInsightsId
    amlWorkspaceId: amlWorkspace.outputs.amlWorkspaceId
    amlWorkspacePrincipalId: amlSupportingModule.outputs.userAssignedIdentityPrincipalId
    computeInstancePrincipalId: computeCluster.outputs.clusterPrincipalId
    userObjectId: userObjectId
    deployRoleAssignments: deployRoleAssignments
  }
}

// Outputs
@description('The login server for the Azure Container Registry')
output acrLoginServer string = acrModule.outputs.acrLoginServer

@description('The name of the Azure Container Registry')
output acrName string = acrModule.outputs.acrName

@description('The name of the Azure Machine Learning workspace')
output amlWorkspaceName string = amlWorkspace.outputs.amlWorkspaceName

@description('The principal ID of the AML workspace managed identity')
output amlWorkspacePrincipalId string = amlWorkspace.outputs.amlWorkspacePrincipalId

@description('The name of the AML compute cluster for pipeline jobs')
output amlComputeClusterName string = computeCluster.outputs.clusterName

@description('The name of the AML storage account')
output amlStorageAccountName string = amlSupportingModule.outputs.amlStorageAccountName

@description('The name of the data storage account')
output dataStorageAccountName string = dataStorageModule.outputs.dataStorageAccountName

@description('The name of the documents container')
output dataStorageContainerName string = dataStorageModule.outputs.documentsContainerName

@description('The name of the key vault used by AML')
output keyVaultName string = amlSupportingModule.outputs.keyVaultName

@description('The resource group name')
output resourceGroupName string = resourceGroup().name

@description('The subscription ID')
output subscriptionId string = subscription().subscriptionId

@description('The name of the embedding endpoint')
output amlEmbeddingEndpointName string = amlEmbeddingEndpoint.outputs.endpointName

@description('The scoring URI of the embedding endpoint')
output amlEmbeddingEndpointScoringUri string = amlEmbeddingEndpoint.outputs.scoringUri

@description('The resource ID of the embedding endpoint')
output amlEmbeddingEndpointId string = amlEmbeddingEndpoint.outputs.endpointId

@description('The instance type for embedding model deployment')
output amlEmbeddingEndpointType string = amlEmbeddingEndpointType

@description('The number of instances for embedding model deployment')
output amlEmbeddingEndpointCount int = amlEmbeddingEndpointCount

@description('The name of the AI Foundry service')
output aiFoundryName string = aiFoundryModule.outputs.aiFoundryName

@description('The endpoint URL of the AI Foundry service')
output aiFoundryEndpoint string = aiFoundryModule.outputs.aiFoundryEndpoint

@description('The principal ID of the AI Foundry managed identity')
output aiFoundryPrincipalId string = aiFoundryModule.outputs.aiFoundryPrincipalId

@description('The name of the AI Project')
output aiProjectName string = aiFoundryModule.outputs.aiProjectName

@description('The name of the deployed GPT-5 Mini model')
output gpt5MiniModelName string = aiFoundryModule.outputs.modelDeploymentName

@description('The AML embedding endpoint scoring URI')
output amlEmbeddingEndpointUrl string = amlEmbeddingEndpoint.outputs.scoringUri

@description('The name of the Container Apps user assigned identity')
output containerAppsIdentityName string = deployContainerApps
  ? containerAppsSupportingModule!.outputs.containerAppsIdentityName
  : ''

@description('The resource ID of the Container Apps user assigned identity')
output containerAppsIdentityId string = deployContainerApps
  ? containerAppsSupportingModule!.outputs.containerAppsIdentityId
  : ''

@description('The principal ID of the Container Apps user assigned identity')
output containerAppsIdentityPrincipalId string = deployContainerApps
  ? containerAppsSupportingModule!.outputs.containerAppsIdentityPrincipalId
  : ''

@description('The name of the QDRANT storage account')
output qdrantStorageAccountName string = deployContainerApps
  ? containerAppsModule!.outputs.qdrantStorageAccountName
  : ''

@description('The name of the Container Apps environment')
output containerAppsEnvironmentName string = deployContainerApps
  ? containerAppsModule!.outputs.containerAppsEnvironmentName
  : ''

@description('The QDRANT HTTP endpoint URL')
output qdrantEndpoint string = deployContainerApps ? 'https://${containerAppsModule!.outputs.qdrantEndpoint}' : ''

@description('The document processor container app endpoint URL')
output docProcessorEndpoint string = deployContainerApps
  ? 'https://${containerAppsModule!.outputs.docProcessorEndpoint}'
  : ''

@description('The Event Grid system topic resource ID')
output eventGridSystemTopicId string = deployContainerApps ? eventGridModule!.outputs.systemTopicId : ''

@description('The Event Grid subscription resource ID')
output eventGridSubscriptionId string = deployContainerApps ? eventGridModule!.outputs.eventSubscriptionId : ''
