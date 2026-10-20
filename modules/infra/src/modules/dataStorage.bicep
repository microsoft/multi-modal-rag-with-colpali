// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('The name of the data storage account')
param dataStorageAccountName string

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

module dataStorageAccount 'br/public:avm/res/storage/storage-account:0.33.1' = {
  name: 'dataStorageAccount'
  params: {
    name: dataStorageAccountName
    location: location
    enableTelemetry: false
    kind: 'StorageV2'
    skuName: storageAccountSku
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    allowSharedKeyAccess: false
    requireInfrastructureEncryption: false
    supportsHttpsTrafficOnly: true
    accessTier: 'Hot'
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Allow'
      bypass: 'AzureServices'
    }
    blobServices: {
      containerDeleteRetentionPolicyEnabled: false
      deleteRetentionPolicyEnabled: false
      containers: [
        {
          name: 'documents'
          publicAccess: 'None'
        }
        {
          name: 'documents-page-images'
          publicAccess: 'None'
        }
        {
          name: 'benchmark-datasets'
          publicAccess: 'None'
        }
        {
          name: 'benchmark-datasets-images'
          publicAccess: 'None'
        }
      ]
    }
  }
}

@description('The resource ID of the data storage account')
output dataStorageAccountId string = dataStorageAccount.outputs.resourceId

@description('The name of the data storage account')
output dataStorageAccountName string = dataStorageAccount.outputs.name

@description('The name of the documents container')
output documentsContainerName string = 'documents'

@description('The name of the benchmark datasets container')
output benchmarkDatasetsContainerName string = 'benchmark-datasets'
