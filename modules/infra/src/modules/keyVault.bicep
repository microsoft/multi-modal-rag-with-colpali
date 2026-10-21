// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('The base name for the Key Vault')
param baseName string

@description('The location for all resources')
param location string = resourceGroup().location

@description('The Application Insights connection string to store securely')
param applicationInsightsConnectionString string

@description('Qdrant API key')
@secure()
param qdrantApiKey string = 'qdrant_${replace(newGuid(), '-', '')}'

@description('Qdrant read-only API key')
@secure()
param qdrantReadOnlyApiKey string = 'qdrant_ro_${replace(newGuid(), '-', '')}'

var keyVaultName = 'kv-${baseName}'

module keyVault 'br/public:avm/res/key-vault/vault:0.14.2' = {
  name: 'keyVault'
  params: {
    name: keyVaultName
    location: location
    enableTelemetry: false
    sku: 'standard'
    enableRbacAuthorization: true
    enableVaultForDeployment: false
    enableVaultForTemplateDeployment: false
    enableVaultForDiskEncryption: false
    enablePurgeProtection: false
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    secrets: [
      {
        name: 'qdrant-api-key'
        value: qdrantApiKey
        contentType: 'text/plain'
        attributes: {
          enabled: true
        }
      }
      {
        name: 'qdrant-readonly-api-key'
        value: qdrantReadOnlyApiKey
        contentType: 'text/plain'
        attributes: {
          enabled: true
        }
      }
      {
        name: 'appinsights-connection-string'
        value: applicationInsightsConnectionString
        contentType: 'text/plain'
        attributes: {
          enabled: true
        }
      }
    ]
  }
}

@description('The name of the Key Vault')
output keyVaultName string = keyVault.outputs.name

@description('The resource ID of the Key Vault')
output keyVaultId string = keyVault.outputs.resourceId

@description('The URI of the Key Vault')
output keyVaultUri string = keyVault.outputs.uri

@description('The name of the Qdrant API key secret')
output qdrantApiKeySecretName string = 'qdrant-api-key'

@description('The name of the Qdrant read-only API key secret')
output qdrantReadOnlyApiKeySecretName string = 'qdrant-readonly-api-key'

@description('The name of the Application Insights connection string secret')
output applicationInsightsConnectionStringSecretName string = 'appinsights-connection-string'
