// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('The name of the AI Foundry service')
param aiFoundryName string

@description('The name of the AI Project')
param aiProjectName string = '${aiFoundryName}-proj'

@description('The location for AI Foundry hub (must be UK South for Agents API availability)')
param aiFoundryLocation string = 'uksouth'

// Hard coded to UK South as AI Foundry Agent Service is not available in North Europe yet
module aiFoundry 'br/public:avm/ptn/ai-ml/ai-foundry:0.7.0' = {
  name: 'aiFoundry'
  params: {
    // Only used for fallback naming; deployed names are supplied explicitly below.
    baseName: 'colpali'
    location: aiFoundryLocation
    enableTelemetry: false
    includeAssociatedResources: false
    aiFoundryConfiguration: {
      accountName: aiFoundryName
      location: aiFoundryLocation
      sku: 'S0'
      allowProjectManagement: true
      disableLocalAuth: true
      createCapabilityHosts: false
      project: {
        name: aiProjectName
        displayName: aiProjectName
        desc: aiProjectName
      }
    }
    aiModelDeployments: [
      {
        name: 'gpt-4.1'
        sku: {
          capacity: 100
          name: 'GlobalStandard'
        }
        model: {
          name: 'gpt-4.1'
          version: '2025-04-14'
          format: 'OpenAI'
        }
      }
    ]
  }
}

// A separate read-only deployment delays resource lookups until the pattern completes.
module deployedResources 'aiFoundryOutputs.bicep' = {
  name: 'aiFoundryOutputs'
  params: {
    aiFoundryName: aiFoundry.outputs.aiServicesName
    aiProjectName: aiFoundry.outputs.aiProjectName
  }
}

@description('The name of the AI Foundry service')
output aiFoundryName string = aiFoundry.outputs.aiServicesName

@description('The resource ID of the AI Foundry service')
output aiFoundryId string = deployedResources.outputs.aiFoundryId

@description('The endpoint URL of the AI Foundry service')
output aiFoundryEndpoint string = deployedResources.outputs.aiFoundryEndpoint

@description('The principal ID of the AI Foundry managed identity')
output aiFoundryPrincipalId string = deployedResources.outputs.aiFoundryPrincipalId

@description('The name of the AI Project')
output aiProjectName string = aiFoundry.outputs.aiProjectName

@description('The resource ID of the AI Project')
output aiProjectId string = deployedResources.outputs.aiProjectId

@description('The principal ID of the AI Project managed identity')
output aiProjectPrincipalId string = deployedResources.outputs.aiProjectPrincipalId

@description('The endpoint URL of the AI Project for AI Agents')
output aiFoundryProjectEndpoint string = deployedResources.outputs.aiFoundryProjectEndpoint

@description('The OpenAI endpoint URL for the AI Foundry service')
output aiFoundryOpenAiEndpoint string = deployedResources.outputs.aiFoundryOpenAiEndpoint

@description('The name of the deployed model')
output modelDeploymentName string = 'gpt-4.1'
