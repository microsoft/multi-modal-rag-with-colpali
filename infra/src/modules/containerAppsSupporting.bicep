@description('The base name for all resources')
param baseName string

@description('The location for all resources')
param location string = resourceGroup().location

@description('The resource ID of the Container Registry to assign ACR Pull permissions')
param containerRegistryId string

// Resource name variables - Following Cloud Adoption Framework (CAF) naming conventions
var containerAppsIdentityName = 'id-ca-${baseName}' // User Assigned Identity for Container Apps

// Built-in Azure role definition IDs
var roles = {
  acrPull: '7f951dda-4ed3-4680-a7ca-43fe172d538d' // AcrPull role
}

// User Assigned Identity for Container Apps (needed for ACR access)
resource containerAppsIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: containerAppsIdentityName
  location: location
}

// Get reference to Container Registry for scoped role assignments
resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: last(split(containerRegistryId, '/'))
}

// Container Apps Identity -> ACR Pull (minimal permission for pulling images)
resource containerAppsAcrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(containerRegistryId, containerAppsIdentity.id, roles.acrPull, 'containerapps-acr-pull')
  scope: containerRegistry
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPull)
    principalId: containerAppsIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Outputs
@description('The name of the Container Apps user assigned identity')
output containerAppsIdentityName string = containerAppsIdentity.name

@description('The resource ID of the Container Apps user assigned identity')
output containerAppsIdentityId string = containerAppsIdentity.id

@description('The principal ID of the Container Apps user assigned identity')
output containerAppsIdentityPrincipalId string = containerAppsIdentity.properties.principalId

@description('The client ID of the Container Apps user assigned identity')
output containerAppsIdentityClientId string = containerAppsIdentity.properties.clientId
