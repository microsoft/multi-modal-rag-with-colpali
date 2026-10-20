// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('The deployed AI Foundry account name')
param aiFoundryName string

@description('The deployed AI Foundry project name')
param aiProjectName string

resource aiFoundryAccount 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: aiFoundryName
}

resource aiProject 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  name: aiProjectName
  parent: aiFoundryAccount
}

@description('The resource ID of the AI Foundry service')
output aiFoundryId string = aiFoundryAccount.id

@description('The endpoint URL of the AI Foundry service')
output aiFoundryEndpoint string = aiFoundryAccount.properties.endpoint

@description('The principal ID of the AI Foundry managed identity')
output aiFoundryPrincipalId string = aiFoundryAccount.identity.principalId

@description('The resource ID of the AI Project')
output aiProjectId string = aiProject.id

@description('The principal ID of the AI Project managed identity')
output aiProjectPrincipalId string = aiProject.identity.principalId

@description('The endpoint URL of the AI Project for AI Agents')
output aiFoundryProjectEndpoint string = 'https://${aiFoundryAccount.properties.customSubDomainName}.services.ai.azure.com/api/projects/${aiProjectName}'

@description('The OpenAI endpoint URL for the AI Foundry service')
output aiFoundryOpenAiEndpoint string = 'https://${aiFoundryAccount.properties.customSubDomainName}.openai.azure.com/'
