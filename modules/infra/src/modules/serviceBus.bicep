// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('The name for the Service Bus namespace')
param serviceBusNamespaceName string

@description('The location for all resources')
param location string = resourceGroup().location

@description('Service Bus SKU')
@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param serviceBusSku string = 'Standard'

var documentProcessingQueueName = 'document-processing'

module serviceBusNamespace 'br/public:avm/res/service-bus/namespace:0.17.1' = {
  name: 'serviceBusNamespace'
  params: {
    name: serviceBusNamespaceName
    location: location
    enableTelemetry: false
    skuObject: {
      name: serviceBusSku
    }
    zoneRedundant: false
    publicNetworkAccess: 'Enabled'
    disableLocalAuth: true
    authorizationRules: []
    queues: [
      {
        name: documentProcessingQueueName
        lockDuration: 'PT5M'
        maxSizeInMegabytes: 1024
        requiresDuplicateDetection: true
        duplicateDetectionHistoryTimeWindow: 'PT10M'
        requiresSession: false
        defaultMessageTimeToLive: 'P14D'
        deadLetteringOnMessageExpiration: true
        maxDeliveryCount: 5
        enableBatchedOperations: true
        autoDeleteOnIdle: 'P10675199DT2H48M5.4775807S'
        enablePartitioning: false
      }
    ]
  }
}

@description('Service Bus namespace resource ID')
output serviceBusNamespaceId string = serviceBusNamespace.outputs.resourceId

@description('Service Bus namespace name')
output serviceBusNamespaceName string = serviceBusNamespace.outputs.name

@description('Service Bus namespace hostname')
output serviceBusNamespaceHostname string = serviceBusNamespace.outputs.serviceBusEndpoint

@description('Document processing queue name')
output documentProcessingQueueName string = documentProcessingQueueName
