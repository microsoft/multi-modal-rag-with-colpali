@description('The name of the Azure Machine Learning workspace')
param amlWorkspaceName string

@description('The location for the AML workspace')
param location string = resourceGroup().location

@description('The sku name for the Azure Machine Learning workspace')
@allowed([
  'Basic'
  'Enterprise'
])
param amlSku string = 'Basic'

@description('The resource ID of the storage account for the AML workspace')
param storageAccountId string

@description('The resource ID of the key vault for the AML workspace')
param keyVaultId string

@description('The resource ID of the application insights for the AML workspace')
param applicationInsightsId string

@description('The resource ID of the container registry for the AML workspace')
param containerRegistryId string

@description('The resource ID of the user-assigned identity for the AML workspace')
param amlWorkspaceIdentityId string

@description('The principal ID of the AML workspace managed identity')
param amlWorkspacePrincipalId string

// Built-in Azure role definition IDs
var roles = {
  keyVaultSecretsOfficer: 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'
}

// Get reference to Key Vault for scoped role assignments
resource keyVault 'Microsoft.KeyVault/vaults@2023-02-01' existing = {
  name: last(split(keyVaultId, '/'))
}

resource uaiKeyVaultSecretsOfficerAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, amlWorkspacePrincipalId, roles.keyVaultSecretsOfficer, 'aml-keyvault')
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.keyVaultSecretsOfficer)
    principalId: amlWorkspacePrincipalId
    principalType: 'ServicePrincipal'
  }
}

resource amlWorkspace 'Microsoft.MachineLearningServices/workspaces@2025-07-01-preview' = {
  name: amlWorkspaceName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${amlWorkspaceIdentityId}': {}
    }
  }
  sku: {
    name: amlSku
    tier: amlSku == 'Basic' ? 'Standard' : 'Premium'
  }
  properties: {
    friendlyName: amlWorkspaceName
    storageAccount: storageAccountId
    keyVault: keyVaultId
    applicationInsights: applicationInsightsId
    containerRegistry: containerRegistryId
    hbiWorkspace: false
    publicNetworkAccess: 'Enabled'
    systemDatastoresAuthMode: 'Identity'
    primaryUserAssignedIdentity: amlWorkspaceIdentityId
  }
}

@description('The ID of the Azure Machine Learning workspace')
output amlWorkspaceId string = amlWorkspace.id

@description('The name of the Azure Machine Learning workspace')
output amlWorkspaceName string = amlWorkspace.name

@description('The principal ID of the AML workspace managed identity')
output amlWorkspacePrincipalId string = amlWorkspacePrincipalId
