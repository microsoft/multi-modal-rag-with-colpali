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

// Get the existing Azure ML workspace resource
resource amlWorkspace 'Microsoft.MachineLearningServices/workspaces@2024-04-01' existing = {
  name: split(amlWorkspaceId, '/')[8]
}

// Create the online endpoint
resource onlineEndpoint 'Microsoft.MachineLearningServices/workspaces/onlineEndpoints@2024-04-01' = {
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

// ------------------------------------------------------------
// OUTPUTS
// ------------------------------------------------------------
@description('The name of the online endpoint')
output endpointName string = onlineEndpoint.name

@description('The resource ID of the online endpoint')
output endpointId string = onlineEndpoint.id

@description('The scoring URI of the online endpoint')
output scoringUri string = onlineEndpoint.properties.scoringUri

@description('The swagger URI for the online endpoint')
output swaggerUri string = onlineEndpoint.properties.swaggerUri

@description('The principal ID of the online endpoint managed identity')
output endpointPrincipalId string = onlineEndpoint.identity.principalId

@description('The provisioning state of the online endpoint')
output provisioningState string = onlineEndpoint.properties.provisioningState
