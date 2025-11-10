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

// Internal implementation details
var documentProcessingQueueName = 'document-processing'

// Create Service Bus Namespace with SAS authentication disabled
resource serviceBusNamespace 'Microsoft.ServiceBus/namespaces@2021-11-01' = {
  name: serviceBusNamespaceName
  location: location
  sku: {
    name: serviceBusSku
    tier: serviceBusSku
  }
  properties: {
    disableLocalAuth: true // Disable SAS authentication, use Azure RBAC instead
  }
}

// Create Queue for document processing
resource documentProcessingQueue 'Microsoft.ServiceBus/namespaces/queues@2021-11-01' = {
  parent: serviceBusNamespace
  name: documentProcessingQueueName
  properties: {
    lockDuration: 'PT5M' // 5 minutes lock duration for message processing
    maxSizeInMegabytes: 1024 // 1GB queue size
    requiresDuplicateDetection: true
    duplicateDetectionHistoryTimeWindow: 'PT10M' // 10 minutes duplicate detection window
    requiresSession: false
    defaultMessageTimeToLive: 'P14D' // 14 days TTL
    deadLetteringOnMessageExpiration: true
    maxDeliveryCount: 5 // Max retry attempts before dead lettering
    enableBatchedOperations: true
    autoDeleteOnIdle: 'P10675199DT2H48M5.4775807S' // Never auto-delete
    enablePartitioning: false
  }
}

// Dead letter queue is automatically created by Service Bus
// Dead letter queue will be named: document-processing/$deadletterqueue
// Note: SAS authentication is disabled, using Azure RBAC for access control instead

@description('Service Bus namespace resource ID')
output serviceBusNamespaceId string = serviceBusNamespace.id

@description('Service Bus namespace name')
output serviceBusNamespaceName string = serviceBusNamespace.name

@description('Service Bus namespace hostname')
output serviceBusNamespaceHostname string = serviceBusNamespace.properties.serviceBusEndpoint

@description('Document processing queue name')
output documentProcessingQueueName string = documentProcessingQueue.name

// Note: Authorization rule outputs removed since SAS authentication is disabled
// Access control is now managed through Azure RBAC
