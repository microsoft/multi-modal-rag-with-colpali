// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('The name of the AI Foundry service')
param aiFoundryName string

@description('The name of the AI Project')
param aiProjectName string = '${aiFoundryName}-proj'

@description('The location for AI Foundry hub (must be UK South for Agents API availability)')
param aiFoundryLocation string = 'uksouth'

// Hard coded to UK South as AI Foundry Agent Service is not available in North Europe yet
resource aiFoundry 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: aiFoundryName
  location: aiFoundryLocation
  identity: {
    type: 'SystemAssigned'
  }
  sku: {
    name: 'S0'
  }
  kind: 'AIServices'
  properties: {
    allowProjectManagement: true
    customSubDomainName: aiFoundryName

    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource aiProject 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  name: aiProjectName
  parent: aiFoundry
  location: aiFoundryLocation
  identity: {
    type: 'SystemAssigned'
  }
  properties: {}
}

resource modelDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aiFoundry
  name: 'gpt-4.1'
  sku: {
    capacity: 100
    name: 'GlobalStandard'
  }
  properties: {
    model: {
      name: 'gpt-4.1'
      version: '2025-04-14'
      format: 'OpenAI'
    }
  }
}

@description('The name of the AI Foundry service')
output aiFoundryName string = aiFoundry.name

@description('The resource ID of the AI Foundry service')
output aiFoundryId string = aiFoundry.id

@description('The endpoint URL of the AI Foundry service')
output aiFoundryEndpoint string = aiFoundry.properties.endpoint

@description('The principal ID of the AI Foundry managed identity')
output aiFoundryPrincipalId string = aiFoundry.identity.principalId

@description('The name of the AI Project')
output aiProjectName string = aiProject.name

@description('The resource ID of the AI Project')
output aiProjectId string = aiProject.id

@description('The principal ID of the AI Project managed identity')
output aiProjectPrincipalId string = aiProject.identity.principalId

@description('The endpoint URL of the AI Project for AI Agents')
output aiFoundryProjectEndpoint string = 'https://${aiFoundry.properties.customSubDomainName}.services.ai.azure.com/api/projects/${aiProject.name}'

@description('The OpenAI endpoint URL for the AI Foundry service')
output aiFoundryOpenAiEndpoint string = 'https://${aiFoundry.properties.customSubDomainName}.openai.azure.com/'

@description('The name of the deployed model')
output modelDeploymentName string = modelDeployment.name
