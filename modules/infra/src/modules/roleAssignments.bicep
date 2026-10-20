// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('Data storage account resource ID for document storage')
param dataStorageAccountId string

@description('AI Foundry service resource ID for role assignments')
param aiFoundryServiceId string

@description('AI Foundry project resource ID for agent role assignments')
param aiFoundryProjectId string

@description('Container registry resource ID for role assignments')
param containerRegistryId string

@description('User object ID for permissions')
param userObjectId string

@description('AKS cluster kubelet identity object ID for ACR pull access')
param aksKubeletIdentityPrincipalId string = ''

@description('AKS Workload identity client ID for application access to storage and service bus')
param aksWorkloadPrincipalId string = ''

@description('Service Bus namespace resource ID for Event Grid access')
param serviceBusNamespaceId string = ''

@description('Key Vault resource ID for secret access')
param keyVaultId string = ''

@description('Flag to control whether to deploy role assignments')
param deployRoleAssignments bool = true

// Built-in Azure role definition IDs
var roles = {
  // General roles
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'

  // Storage roles
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  storageBlobDataReader: '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

  storageAccountContributor: '17d1049b-9a84-46fb-8f53-869881c3d3ab'

  // AI Services roles
  azureAIUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  cognitiveServicesOpenAIUser: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

  // ACR roles
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  acrPush: '8311e382-0749-4cb8-b61a-304f252e45ec'

  // Application Insights roles
  applicationInsightsComponentContributor: 'ae349356-3a1b-4a5e-921d-050484c6347e'

  // Service Bus roles
  serviceBusDataSender: '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
  serviceBusDataReceiver: '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'

  // Key Vault roles
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
}

// =============================================================================
// AKS IDENTITY PERMISSIONS
// =============================================================================

// AKS Workload Identity -> Data Storage (for blob access)
module aksDataStorageAssignment 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = if (deployRoleAssignments && !empty(aksWorkloadPrincipalId)) {
  name: 'aksDataStorageAssignment'
  params: {
    name: guid(dataStorageAccountId, aksWorkloadPrincipalId, roles.storageBlobDataContributor, 'aks-data-storage')
    resourceId: dataStorageAccountId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.storageBlobDataContributor
    )
    principalId: aksWorkloadPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AKS Workload Identity -> Service Bus Namespace (for sending messages)
module aksServiceBusSenderAssignment 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = if (deployRoleAssignments && !empty(aksWorkloadPrincipalId) && !empty(serviceBusNamespaceId)) {
  name: 'aksServiceBusSenderAssignment'
  params: {
    name: guid(serviceBusNamespaceId, aksWorkloadPrincipalId, roles.serviceBusDataSender, 'aks-servicebus-sender')
    resourceId: serviceBusNamespaceId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataSender)
    principalId: aksWorkloadPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AKS Workload Identity -> Service Bus Namespace (for receiving messages)
module aksServiceBusReceiverAssignment 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = if (deployRoleAssignments && !empty(aksWorkloadPrincipalId) && !empty(serviceBusNamespaceId)) {
  name: 'aksServiceBusReceiverAssignment'
  params: {
    name: guid(serviceBusNamespaceId, aksWorkloadPrincipalId, roles.serviceBusDataReceiver, 'aks-servicebus-receiver')
    resourceId: serviceBusNamespaceId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataReceiver)
    principalId: aksWorkloadPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AKS Cluster Kubelet Identity -> Container Registry (for pulling images)
module aksClusterAcrPullAssignment 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = if (deployRoleAssignments && !empty(aksKubeletIdentityPrincipalId)) {
  name: 'aksClusterAcrPullAssignment'
  params: {
    name: guid(containerRegistryId, aksKubeletIdentityPrincipalId, roles.acrPull, 'aks-cluster-acr-pull')
    resourceId: containerRegistryId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: aksKubeletIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AKS Workload Identity -> Key Vault (for reading Qdrant API key secrets)
module aksKeyVaultSecretsUserAssignment 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = if (deployRoleAssignments && !empty(aksWorkloadPrincipalId) && !empty(keyVaultId)) {
  name: 'aksKeyVaultSecretsUserAssignment'
  params: {
    name: guid(keyVaultId, aksWorkloadPrincipalId, roles.keyVaultSecretsUser, 'aks-keyvault-secrets-user')
    resourceId: keyVaultId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    principalId: aksWorkloadPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AKS Workload Identity -> AI Foundry Project (for Azure AI Agents API access)
module aksAIFoundryProjectAssignment 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = if (deployRoleAssignments && !empty(aksWorkloadPrincipalId)) {
  name: 'aksAIFoundryProjectAssignment'
  params: {
    name: guid(aiFoundryProjectId, aksWorkloadPrincipalId, roles.azureAIUser, 'aks-ai-foundry-project')
    resourceId: aiFoundryProjectId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAIUser)
    principalId: aksWorkloadPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AKS Workload Identity -> AI Foundry Service (for OpenAI model access)
module aksAIFoundryOpenAIAssignment 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = if (deployRoleAssignments && !empty(aksWorkloadPrincipalId)) {
  name: 'aksAIFoundryOpenAIAssignment'
  params: {
    name: guid(aiFoundryServiceId, aksWorkloadPrincipalId, roles.cognitiveServicesOpenAIUser, 'aks-ai-foundry-openai')
    resourceId: aiFoundryServiceId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.cognitiveServicesOpenAIUser
    )
    principalId: aksWorkloadPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// =============================================================================
// USER PERMISSIONS FOR STORAGE
// =============================================================================

// User -> Data Storage Account (for reading/writing documents)
module userDataStorageAssignment 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = if (deployRoleAssignments && !empty(userObjectId)) {
  name: 'userDataStorageAssignment'
  params: {
    name: guid(dataStorageAccountId, userObjectId, roles.storageBlobDataContributor, 'user-data-storage')
    resourceId: dataStorageAccountId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.storageBlobDataContributor
    )
    principalId: userObjectId
    principalType: 'User'
  }
}

// User -> AI Foundry (for AI services access)
module userAIFoundryAssignment 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = if (deployRoleAssignments && !empty(userObjectId)) {
  name: 'userAIFoundryAssignment'
  params: {
    name: guid(aiFoundryServiceId, userObjectId, roles.azureAIUser, 'user-ai-foundry')
    resourceId: aiFoundryServiceId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAIUser)
    principalId: userObjectId
    principalType: 'User'
  }
}

// User -> Container Registry (for pushing images)
module userAcrPushAssignment 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = if (deployRoleAssignments && !empty(userObjectId)) {
  name: 'userAcrPushAssignment'
  params: {
    name: guid(containerRegistryId, userObjectId, roles.acrPush, 'user-acr-push')
    resourceId: containerRegistryId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPush)
    principalId: userObjectId
    principalType: 'User'
  }
}

// User -> Container Registry (for pulling images)
module userAcrPullAssignment 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = if (deployRoleAssignments && !empty(userObjectId)) {
  name: 'userAcrPullAssignment'
  params: {
    name: guid(containerRegistryId, userObjectId, roles.acrPull, 'user-acr-pull')
    resourceId: containerRegistryId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: userObjectId
    principalType: 'User'
  }
}
