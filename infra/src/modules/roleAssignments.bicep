@description('AML storage account resource ID for ML workspace artifacts')
param amlStorageAccountId string

@description('Data storage account resource ID for document storage')
param dataStorageAccountId string

@description('Key vault resource ID for role assignments')
param keyVaultId string

@description('AI Foundry service resource ID for role assignments')
param aiFoundryServiceId string

@description('Container registry resource ID for role assignments')
param containerRegistryId string

@description('Application Insights resource ID for role assignments')
param applicationInsightsId string

@description('Azure ML workspace resource ID for endpoint access')
param amlWorkspaceId string

@description('Azure ML workspace principal ID')
param amlWorkspacePrincipalId string

@description('Compute instance principal ID (optional - empty if no compute instance)')
param computeInstancePrincipalId string = ''

@description('User object ID for permissions')
param userObjectId string

@description('Container Apps user assigned identity principal ID for data storage access')
param containerAppsIdentityPrincipalId string = ''

@description('Flag to control whether to deploy role assignments')
param deployRoleAssignments bool = true

// Built-in Azure role definition IDs
var roles = {
  // General roles
  contributor: 'b24988ac-6180-42a0-ab88-20f7382dd24c'

  // Storage roles
  storageBlobDataOwner: 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
  storageBlobDataContributor: 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
  storageFileDataPrivilegedContributor: '69566ab7-960f-475b-8e7c-b3118f30c6bd'
  storageAccountContributor: '17d1049b-9a84-46fb-8f53-869881c3d3ab'

  // Key Vault roles
  keyVaultSecretsUser: '4633458b-17de-408a-b874-0445c86b69e6'
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
  keyVaultAdministrator: '00482a5a-887f-4fb3-b363-3b7fe8e74483'

  // Azure ML roles
  amlDataScientist: 'f6c7c914-8db3-469d-8ca1-694a8f32e121'
  amlComputeOperator: 'e661802a-a43b-11e9-b6c7-4c9a5ed0059e'
  amlWorkspaceConnection: '3636dbd8-5c3b-4c3d-b1f2-b50b44f7f7f8' // AzureML Workspace Connection Secrets Reader

  // AI Services roles
  azureAIUser: '53ca6127-db72-4b80-b1b0-d745d6d5456d'

  // ACR roles
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d'
  acrPush: '8311e382-0749-4cb8-b61a-304f252e45ec'

  // Application Insights roles
  applicationInsightsComponentContributor: 'ae349356-3a1b-4a5e-921d-050484c6347e'
}

// Get reference to AML workspace for scoped role assignments
resource amlWorkspace 'Microsoft.MachineLearningServices/workspaces@2024-04-01' existing = {
  name: last(split(amlWorkspaceId, '/'))
}

// Get reference to Key Vault for scoped role assignments
resource keyVault 'Microsoft.KeyVault/vaults@2023-02-01' existing = {
  name: last(split(keyVaultId, '/'))
}

resource dataStorageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: last(split(dataStorageAccountId, '/'))
}

// Get reference to AML storage account for scoped role assignments
resource amlStorageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' existing = {
  name: last(split(amlStorageAccountId, '/'))
}

// Get reference to AI Foundry service for scoped role assignment
resource aiFoundryService 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: last(split(aiFoundryServiceId, '/'))
}

// Get reference to Container Registry for scoped role assignments
resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: last(split(containerRegistryId, '/'))
}

// Get reference to Application Insights for scoped role assignments
resource applicationInsights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: last(split(applicationInsightsId, '/'))
}

// =============================================================================
// AZURE ML WORKSPACE PERMISSIONS
// =============================================================================

// AML Workspace -> AML Storage Account (for ML artifacts)
resource amlWorkspaceStorageAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(amlWorkspacePrincipalId)) {
  name: guid(amlStorageAccountId, amlWorkspacePrincipalId, roles.storageBlobDataContributor, 'aml-workspace-storage')
  scope: amlStorageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.storageBlobDataContributor
    )
    principalId: amlWorkspacePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AML Workspace Identity -> AML Workspace (Contributor role for workspace management)
resource amlWorkspaceContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(amlWorkspacePrincipalId)) {
  name: guid(amlWorkspaceId, amlWorkspacePrincipalId, roles.contributor, 'aml-workspace-contributor')
  scope: amlWorkspace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
    principalId: amlWorkspacePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AML Workspace Identity -> AML Storage Account (Contributor role for control plane operations)
resource amlWorkspaceStorageContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(amlWorkspacePrincipalId)) {
  name: guid(amlStorageAccountId, amlWorkspacePrincipalId, roles.contributor, 'aml-workspace-storage-contributor')
  scope: amlStorageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
    principalId: amlWorkspacePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AML Workspace Identity -> Key Vault (Contributor role for control plane operations)
resource amlWorkspaceKeyVaultContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(amlWorkspacePrincipalId)) {
  name: guid(keyVaultId, amlWorkspacePrincipalId, roles.contributor, 'aml-workspace-keyvault-contributor')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
    principalId: amlWorkspacePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AML Workspace Identity -> Key Vault (Key Vault Administrator role for data plane operations)
resource amlWorkspaceKeyVaultAdminAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(amlWorkspacePrincipalId)) {
  name: guid(keyVaultId, amlWorkspacePrincipalId, roles.keyVaultAdministrator, 'aml-workspace-keyvault-admin')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultAdministrator)
    principalId: amlWorkspacePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AML Workspace Identity -> Container Registry (Contributor role for registry management)
resource amlWorkspaceAcrContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(amlWorkspacePrincipalId)) {
  name: guid(containerRegistryId, amlWorkspacePrincipalId, roles.contributor, 'aml-workspace-acr-contributor')
  scope: containerRegistry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
    principalId: amlWorkspacePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// AML Workspace Identity -> Application Insights (Contributor role for monitoring)
resource amlWorkspaceAppInsightsContributorAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(amlWorkspacePrincipalId)) {
  name: guid(applicationInsightsId, amlWorkspacePrincipalId, roles.contributor, 'aml-workspace-appinsights-contributor')
  scope: applicationInsights
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.contributor)
    principalId: amlWorkspacePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// =============================================================================
// COMPUTE INSTANCE PERMISSIONS
// =============================================================================

// Compute Instance -> AML Storage Account (for datasets and models)
resource computeInstanceStorageAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(computeInstancePrincipalId)) {
  name: guid(
    amlStorageAccountId,
    computeInstancePrincipalId,
    roles.storageBlobDataContributor,
    'compute-cluster-aml-storage'
  )
  scope: amlStorageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.storageBlobDataContributor
    )
    principalId: computeInstancePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Compute Instance -> Key Vault (for accessing secrets in notebooks)
resource computeInstanceKeyVaultAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(computeInstancePrincipalId)) {
  name: guid(keyVaultId, computeInstancePrincipalId, roles.keyVaultSecretsUser, 'compute-keyvault')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsUser)
    principalId: computeInstancePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Assign ACR Push role to AML Workspace (for pushing images)
resource amlWorkspaceAcrPushAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(amlWorkspacePrincipalId)) {
  name: guid(amlWorkspaceId, amlWorkspacePrincipalId, roles.acrPush, 'aml-workspace-acr-push')
  scope: containerRegistry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPush)
    principalId: amlWorkspacePrincipalId
    principalType: 'ServicePrincipal'
  }
}

// =============================================================================
// CONTAINER APPS PERMISSIONS
// =============================================================================

// Container Apps Identity -> Data Storage Account (for blob access from document processor)
resource containerAppsDataStorageAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(containerAppsIdentityPrincipalId)) {
  name: guid(
    dataStorageAccountId,
    containerAppsIdentityPrincipalId,
    roles.storageBlobDataContributor,
    'containerapps-data-storage'
  )
  scope: dataStorageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.storageBlobDataContributor
    )
    principalId: containerAppsIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// Container Apps Identity -> AML Workspace (for calling online endpoints)
resource containerAppsAmlWorkspaceAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(containerAppsIdentityPrincipalId)) {
  name: guid(amlWorkspaceId, containerAppsIdentityPrincipalId, roles.amlDataScientist, 'containerapps-aml-workspace')
  scope: amlWorkspace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.amlDataScientist)
    principalId: containerAppsIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// =============================================================================
// USER PERMISSIONS FOR STORAGE
// =============================================================================

// User -> Data Storage Account (for reading/writing documents)
resource userDataStorageAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(userObjectId)) {
  name: guid(dataStorageAccountId, userObjectId, roles.storageBlobDataContributor, 'user-data-storage')
  scope: dataStorageAccount
  properties: {
    roleDefinitionId: subscriptionResourceId(
      'Microsoft.Authorization/roleDefinitions',
      roles.storageBlobDataContributor
    )
    principalId: userObjectId
    principalType: 'User'
  }
}

// User -> AI Foundry (for AI services access)
resource userAIFoundryAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments && !empty(userObjectId)) {
  name: guid(aiFoundryServiceId, userObjectId, roles.azureAIUser, 'user-ai-foundry')
  scope: aiFoundryService
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.azureAIUser)
    principalId: userObjectId
    principalType: 'User'
  }
}
