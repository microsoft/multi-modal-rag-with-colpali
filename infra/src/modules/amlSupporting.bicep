@description('Base name for all resources')
param baseName string

@description('The location for all resources')
param location string = resourceGroup().location

@description('The SKU name for the storage account')
@allowed([
  'Standard_LRS'
  'Standard_GRS'
  'Standard_RAGRS'
  'Standard_ZRS'
  'Premium_LRS'
  'Premium_ZRS'
  'Standard_GZRS'
  'Standard_RAGZRS'
])
param storageAccountSku string = 'Standard_LRS'

@description('The SKU name for the Key Vault')
@allowed([
  'standard'
  'premium'
])
param keyVaultSku string = 'standard'

@description('The retention period in days for Application Insights data')
param applicationInsightsRetentionInDays int = 90

// Resource name variables - Following Cloud Adoption Framework (CAF) naming conventions
var amlStorageAccountName = replace('staml${baseName}', '-', '') // Storage Account: CAF standard 'st' + workload
var keyVaultName = take('kv-${baseName}', 24) // Key Vault: CAF standard 'kv', limited to 24 chars
var applicationInsightsName = 'appi-${baseName}' // Application Insights: CAF standard 'appi'
var userAssignedIdentityName = 'id-mlw-${baseName}' // User Assigned Identity: CAF standard 'id'

// Create AML Storage Account (for Azure ML workspace)
resource amlStorageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: amlStorageAccountName
  location: location
  kind: 'StorageV2'
  sku: {
    name: storageAccountSku
  }
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    supportsHttpsTrafficOnly: true
    accessTier: 'Hot'
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow' // Allow public access from all networks
      bypass: 'AzureServices' // Allow Azure services to access this storage account
    }
    encryption: {
      services: {
        blob: {
          enabled: true
        }
        file: {
          enabled: true
        }
      }
      keySource: 'Microsoft.Storage'
    }
  }
}

// Create User Assigned Identity for AML Workspace
resource userAssignedIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: userAssignedIdentityName
  location: location
}

// Create Key Vault
resource keyVault 'Microsoft.KeyVault/vaults@2023-02-01' = {
  name: keyVaultName
  location: location
  properties: {
    sku: {
      family: 'A'
      name: keyVaultSku
    }
    tenantId: subscription().tenantId
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: true
    enableRbacAuthorization: true
    publicNetworkAccess: 'Enabled'
  }
}

// Create Application Insights
resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: applicationInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    RetentionInDays: applicationInsightsRetentionInDays
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

// ------------------------------------------------------------
// OUTPUTS
// ------------------------------------------------------------
@description('The resource ID of the AML storage account')
output amlStorageAccountId string = amlStorageAccount.id

@description('The name of the AML storage account')
output amlStorageAccountName string = amlStorageAccount.name

@description('The resource ID of the key vault')
output keyVaultId string = keyVault.id

@description('The name of the key vault')
output keyVaultName string = keyVault.name

@description('The resource ID of the application insights')
output applicationInsightsId string = applicationInsights.id

@description('The name of the application insights')
output applicationInsightsName string = applicationInsights.name

@description('The resource ID of the user-assigned identity for AML')
output userAssignedIdentityId string = userAssignedIdentity.id

@description('The principal ID of the user-assigned identity for AML')
output userAssignedIdentityPrincipalId string = userAssignedIdentity.properties.principalId

@description('The client ID of the user-assigned identity for AML')
output userAssignedIdentityClientId string = userAssignedIdentity.properties.clientId
