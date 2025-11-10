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

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
  }
}

resource qdrantApiKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'qdrant-api-key'
  properties: {
    value: qdrantApiKey
    contentType: 'text/plain'
    attributes: {
      enabled: true
    }
  }
}

resource qdrantReadOnlyApiKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'qdrant-readonly-api-key'
  properties: {
    value: qdrantReadOnlyApiKey
    contentType: 'text/plain'
    attributes: {
      enabled: true
    }
  }
}

resource applicationInsightsConnectionStringSecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: keyVault
  name: 'appinsights-connection-string'
  properties: {
    value: applicationInsightsConnectionString
    contentType: 'text/plain'
    attributes: {
      enabled: true
    }
  }
}

@description('The name of the Key Vault')
output keyVaultName string = keyVault.name

@description('The resource ID of the Key Vault')
output keyVaultId string = keyVault.id

@description('The URI of the Key Vault')
output keyVaultUri string = keyVault.properties.vaultUri

@description('The name of the Qdrant API key secret')
output qdrantApiKeySecretName string = qdrantApiKeySecret.name

@description('The name of the Qdrant read-only API key secret')
output qdrantReadOnlyApiKeySecretName string = qdrantReadOnlyApiKeySecret.name

@description('The name of the Application Insights connection string secret')
output applicationInsightsConnectionStringSecretName string = applicationInsightsConnectionStringSecret.name
