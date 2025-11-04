@description('The base name for the container apps resources')
param baseName string

@description('The location for all resources')
param location string = resourceGroup().location

@description('The SKU for the QDRANT storage account')
@allowed([
  'Premium_LRS'
  'Premium_ZRS'
])
param storageSku string = 'Premium_LRS'

@description('The container registry name for pulling images')
param containerRegistryName string

@description('The ColPali endpoint URL for document processing')
param colpaliEndpointUrl string = ''

@description('The data storage account name for blob access')
param dataStorageAccountName string

@description('The resource ID of the user assigned identity for Container Apps')
param containerAppsIdentityId string

@description('The client ID of the user assigned identity for Container Apps')
param containerAppsIdentityClientId string

@description('The resource ID of the existing Application Insights Log Analytics workspace')
param logAnalyticsWorkspaceId string

@description('The image tag for the document processor container')
param documentProcessorImageTag string = 'latest'

var environmentName = 'cae-${baseName}'
var qdrantStorageName = replace('stqdrant${baseName}', '-', '')
var qdrantShareName = 'qdrantazfiles'
var qdrantContainerAppName = 'ca-qdrant-${baseName}'
var docProcessorContainerAppName = 'ca-docproc-${baseName}'

// QDRANT Storage Account
resource qdrantStorageAccount 'Microsoft.Storage/storageAccounts@2021-09-01' = {
  name: qdrantStorageName
  location: location
  sku: {
    name: storageSku
  }
  kind: 'FileStorage'
  properties: {
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
  }
}

// QDRANT File Share
resource qdrantFileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2021-09-01' = {
  name: '${qdrantStorageName}/default/${qdrantShareName}'
  dependsOn: [
    qdrantStorageAccount
  ]
  properties: {
    shareQuota: 1024 // 1TB quota
  }
}

resource containerAppsEnvironment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: environmentName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: reference(logAnalyticsWorkspaceId, '2020-08-01').customerId
        sharedKey: listKeys(logAnalyticsWorkspaceId, '2020-08-01').primarySharedKey
      }
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
}

// QDRANT Storage Mount
resource qdrantStorageMount 'Microsoft.App/managedEnvironments/storages@2023-05-01' = {
  name: 'qdrantstoragemount'
  parent: containerAppsEnvironment
  dependsOn: [
    qdrantFileShare
  ]
  properties: {
    azureFile: {
      accountName: qdrantStorageName
      shareName: qdrantShareName
      accountKey: qdrantStorageAccount.listKeys().keys[0].value
      accessMode: 'ReadWrite'
    }
  }
}

resource qdrantContainerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: qdrantContainerAppName
  location: location
  dependsOn: [
    qdrantStorageMount
  ]
  properties: {
    environmentId: containerAppsEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 6333
        corsPolicy: {
          allowedOrigins: ['*']
          allowedMethods: ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS']
          allowedHeaders: ['*']
        }
      }
    }
    workloadProfileName: 'Consumption'
    template: {
      containers: [
        {
          name: 'qdrant-http'
          image: 'qdrant/qdrant:latest'
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          env: [
            {
              name: 'QDRANT__SERVICE__HTTP_PORT'
              value: '6333'
            }
            {
              name: 'QDRANT__SERVICE__GRPC_PORT'
              value: '6334'
            }
          ]
          volumeMounts: [
            {
              volumeName: 'qdrantstoragevol'
              mountPath: '/qdrant/storage'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 3
      }
      volumes: [
        {
          name: 'qdrantstoragevol'
          storageName: 'qdrantstoragemount'
          storageType: 'AzureFile'
        }
      ]
    }
  }
}

resource docProcessorContainerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: docProcessorContainerAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${containerAppsIdentityId}': {}
    }
  }
  properties: {
    environmentId: containerAppsEnvironment.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        allowInsecure: false
      }
      registries: [
        {
          server: '${containerRegistryName}.azurecr.io'
          identity: containerAppsIdentityId
        }
      ]
    }
    workloadProfileName: 'Consumption'
    template: {
      containers: [
        {
          name: 'document-processor'
          image: '${containerRegistryName}.azurecr.io/document-processor:${documentProcessorImageTag}'
          env: [
            {
              name: 'AML_EMBEDDING_ENDPOINT_URL'
              value: colpaliEndpointUrl
            }
            {
              name: 'COLPALI_REQUEST_TIMEOUT'
              value: '120'
            }
            {
              name: 'COLPALI_MAX_IMAGE_SIZE'
              value: '1536'
            }
            {
              name: 'COLPALI_MAX_CONCURRENT_REQUESTS'
              value: '5'
            }
            // Storage and other services
            {
              name: 'QDRANT_ENDPOINT'
              value: 'https://${qdrantContainerApp.properties.configuration.ingress.fqdn}'
            }
            {
              name: 'QDRANT_COLLECTION_NAME'
              value: 'colpali-documents'
            }
            {
              name: 'DATA_STORAGE_ACCOUNT_NAME'
              value: dataStorageAccountName
            }
            {
              name: 'AZURE_CLIENT_ID'
              value: containerAppsIdentityClientId
            }
          ]
          resources: {
            cpu: json('1.0')
            memory: '2Gi'
          }
          probes: [
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 30
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: 8000
                scheme: 'HTTP'
              }
              initialDelaySeconds: 10
              periodSeconds: 5
              timeoutSeconds: 3
              failureThreshold: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1 // Keep at least one replica running for Event Grid validation
        maxReplicas: 10 // Scale up for batch processing
      }
    }
  }
}

// Container Apps built-in logging with azure-monitor destination
// Logs will automatically flow to Application Insights
// No additional diagnostic settings needed - Container Apps handles this natively

// Outputs
@description('The name of the QDRANT storage account')
output qdrantStorageAccountName string = qdrantStorageAccount.name

@description('The resource ID of the QDRANT storage account')
output qdrantStorageAccountId string = qdrantStorageAccount.id

@description('The name of the QDRANT file share')
output qdrantFileShareName string = qdrantShareName

@description('The name of the Container Apps environment')
output containerAppsEnvironmentName string = containerAppsEnvironment.name

@description('The ID of the Container Apps environment')
output containerAppsEnvironmentId string = containerAppsEnvironment.id

@description('The name of the QDRANT container app')
output qdrantContainerAppName string = qdrantContainerApp.name

@description('The FQDN of the QDRANT container app')
output qdrantEndpoint string = qdrantContainerApp.properties.configuration.ingress.fqdn

@description('The name of the document processor container app')
output docProcessorContainerAppName string = docProcessorContainerApp.name

@description('The FQDN of the document processor container app')
output docProcessorEndpoint string = docProcessorContainerApp.properties.configuration.ingress.fqdn

@description('Container Apps use built-in azure-monitor logging')
output loggingInfo string = 'Container Apps logs available in Azure Portal under Monitoring > Logs'
