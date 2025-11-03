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

// Resource name variables - Following Cloud Adoption Framework (CAF) naming conventions
var dataStorageAccountName = replace('stdata${baseName}', '-', '') // Storage Account: CAF standard 'st' + workload

// Create Data Storage Account (for document storage and processing)
resource dataStorageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: dataStorageAccountName
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
      defaultAction: 'Allow'
      bypass: 'AzureServices'
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

// Blob service for data storage
resource dataBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: dataStorageAccount
  name: 'default'
}

// Container for documents (this is what the blob trigger watches)
resource documentsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: dataBlobService
  name: 'documents'
  properties: {
    publicAccess: 'None'
  }
}

// ------------------------------------------------------------
// OUTPUTS
// ------------------------------------------------------------
@description('The resource ID of the data storage account')
output dataStorageAccountId string = dataStorageAccount.id

@description('The name of the data storage account')
output dataStorageAccountName string = dataStorageAccount.name

@description('The name of the documents container')
output documentsContainerName string = documentsContainer.name
