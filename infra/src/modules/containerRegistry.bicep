// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('The name of the Azure Container Registry')
param acrName string

@description('The location for the ACR')
param location string = resourceGroup().location

@description('The SKU name for the Azure Container Registry')
@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param acrSku string = 'Basic'

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: acrSku
  }
  properties: {
    adminUserEnabled: false
    dataEndpointEnabled: false
    publicNetworkAccess: 'Enabled'
    networkRuleBypassOptions: 'AzureServices'
    zoneRedundancy: 'Disabled'
  }
}

@description('The login server for the Azure Container Registry')
output acrLoginServer string = containerRegistry.properties.loginServer

@description('The name of the Azure Container Registry')
output acrName string = containerRegistry.name

@description('The resource ID of the Azure Container Registry')
output acrId string = containerRegistry.id
