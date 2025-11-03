@description('The name of the compute instance (used to name the identity)')
param computeInstanceName string

@description('The location for the managed identity')
param location string = resourceGroup().location

// Create user-assigned managed identity for the compute instance
resource computeInstanceIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${computeInstanceName}' // Managed Identity: CAF standard 'id'
  location: location
}

// ------------------------------------------------------------
// OUTPUTS
// ------------------------------------------------------------
@description('The principal ID of the compute instance managed identity')
output principalId string = computeInstanceIdentity.properties.principalId

@description('The resource ID of the compute instance managed identity')
output identityId string = computeInstanceIdentity.id

@description('The client ID of the compute instance managed identity')
output clientId string = computeInstanceIdentity.properties.clientId
