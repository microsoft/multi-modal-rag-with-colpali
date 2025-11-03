@description('Base name for all resources')
param baseName string

@description('The location for all resources')
param location string = resourceGroup().location

@description('The data storage account ID to subscribe to')
param dataStorageAccountId string

@description('The data storage account name')
param dataStorageAccountName string

@description('The container app endpoint URL for webhooks')
@secure()
param containerAppWebhookUrl string

// Event Grid System Topic (automatically handles blob events)
resource blobSystemTopic 'Microsoft.EventGrid/systemTopics@2024-06-01-preview' = {
  name: 'egst-${dataStorageAccountName}-blobs' // CAF: Event Grid System Topic
  location: location
  properties: {
    source: dataStorageAccountId
    topicType: 'Microsoft.Storage.StorageAccounts'
  }
}

// Event Subscription (blob created → container app webhook)
resource blobEventSubscription 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2024-06-01-preview' = {
  parent: blobSystemTopic
  name: 'egs-${baseName}-document-processing' // CAF: Event Grid Subscription
  properties: {
    destination: {
      endpointType: 'WebHook'
      properties: {
        endpointUrl: containerAppWebhookUrl
        maxEventsPerBatch: 1
        preferredBatchSizeInKilobytes: 64
      }
    }
    filter: {
      includedEventTypes: [
        'Microsoft.Storage.BlobCreated'
      ]
      subjectBeginsWith: '/blobServices/default/containers/documents/blobs/'
      subjectEndsWith: '.pdf'
    }
    retryPolicy: {
      maxDeliveryAttempts: 30
      eventTimeToLiveInMinutes: 1440
    }
    eventDeliverySchema: 'EventGridSchema'
  }
}

@description('The Event Grid System Topic resource ID')
output systemTopicId string = blobSystemTopic.id

@description('The Event Grid subscription resource ID')
output eventSubscriptionId string = blobEventSubscription.id
