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

resource blobSystemTopic 'Microsoft.EventGrid/systemTopics@2024-06-01-preview' = {
  name: 'egst-${dataStorageAccountName}-blobs'
  location: location
  properties: {
    source: dataStorageAccountId
    topicType: 'Microsoft.Storage.StorageAccounts'
  }
}

resource blobEventSubscription 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2024-06-01-preview' = {
  parent: blobSystemTopic
  name: 'egs-${baseName}-document-processing'
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
        'Microsoft.Storage.BlobDeleted'
      ]
      subjectBeginsWith: '/blobServices/default/containers/documents/blobs/'
      subjectEndsWith: '.pdf'
    }
    retryPolicy: {
      maxDeliveryAttempts: 5
      eventTimeToLiveInMinutes: 180
    }
    eventDeliverySchema: 'EventGridSchema'
  }
}

@description('The Event Grid System Topic resource ID')
output systemTopicId string = blobSystemTopic.id

@description('The Event Grid subscription resource ID')
output eventSubscriptionId string = blobEventSubscription.id
