@description('The name for the Azure ML online endpoint')
param endpointName string

@description('The location for the online endpoint')
param location string = resourceGroup().location

@description('The resource ID of the Azure ML workspace')
param amlWorkspaceId string

@description('The description for the online endpoint')
param endpointDescription string = 'ColPali document understanding inference endpoint'

@description('Tags for the endpoint')
param tags object = {}

@description('Whether to create the endpoint. Auto-detected by deployment scripts.')
param createEndpoint bool = true

// Get the existing Azure ML workspace resource
resource amlWorkspace 'Microsoft.MachineLearningServices/workspaces@2024-04-01' existing = {
  name: split(amlWorkspaceId, '/')[8]
}

// Create the online endpoint only if it doesn't already exist
resource onlineEndpoint 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints@2024-04-01' = if (createEndpoint) {
  parent: amlWorkspace
  name: endpointName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    description: endpointDescription
    authMode: 'AADToken'
    publicNetworkAccess: 'Enabled'
  }
}

// Reference existing endpoint only when not creating a new one
resource existingEndpoint 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints@2024-04-01' existing = if (!createEndpoint) {
  parent: amlWorkspace
  name: endpointName
}

// ------------------------------------------------------------
// OUTPUTS
// ------------------------------------------------------------
@description('The name of the online endpoint')
output endpointName string = endpointName

@description('The resource ID of the online endpoint')
output endpointId string = createEndpoint ? onlineEndpoint!.id : existingEndpoint!.id

@description('The scoring URI of the online endpoint')
output scoringUri string = createEndpoint
  ? onlineEndpoint!.properties.scoringUri
  : existingEndpoint!.properties.scoringUri

@description('The swagger URI for the online endpoint')
output swaggerUri string = createEndpoint
  ? onlineEndpoint!.properties.swaggerUri
  : existingEndpoint!.properties.swaggerUri

@description('The principal ID of the online endpoint managed identity')
output endpointPrincipalId string = createEndpoint
  ? onlineEndpoint!.identity.principalId
  : existingEndpoint!.identity.principalId

@description('The provisioning state of the online endpoint')
output provisioningState string = createEndpoint
  ? onlineEndpoint!.properties.provisioningState
  : existingEndpoint!.properties.provisioningState
