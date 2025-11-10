@description('Base name for all resources')
param baseName string

@description('The location for all resources')
param location string = resourceGroup().location

@description('The data storage account ID to subscribe to')
param dataStorageAccountId string

@description('The data storage account name')
param dataStorageAccountName string

@description('Service Bus namespace resource ID')
param serviceBusNamespaceId string

@description('Service Bus queue name for document processing')
param serviceBusQueueName string

// Built-in Azure role definition IDs
var roles = {
  // Service Bus roles
  serviceBusDataSender: '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
  serviceBusDataReceiver: '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
}

// Internal resource names
var systemTopicName = 'egst-${dataStorageAccountName}-blobs'
var eventGridIdentityName = 'id-${systemTopicName}'

// Create User Assigned Identity for Event Grid
resource eventGridIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: eventGridIdentityName
  location: location
}

resource blobSystemTopic 'Microsoft.EventGrid/systemTopics@2024-06-01-preview' = {
  name: systemTopicName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${eventGridIdentity.id}': {}
    }
  }
  properties: {
    source: dataStorageAccountId
    topicType: 'Microsoft.Storage.StorageAccounts'
  }
}

resource blobEventSubscription 'Microsoft.EventGrid/systemTopics/eventSubscriptions@2024-06-01-preview' = {
  parent: blobSystemTopic
  name: 'egs-${baseName}-document-processing'
  properties: {
    deliveryWithResourceIdentity: {
      destination: {
        endpointType: 'ServiceBusQueue'
        properties: {
          resourceId: '${serviceBusNamespaceId}/queues/${serviceBusQueueName}'
        }
      }
      identity: {
        type: 'UserAssigned'
        userAssignedIdentity: eventGridIdentity.id
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
  dependsOn: [
    eventGridServiceBusDataSenderRole
    eventGridServiceBusDataReceiverRole
  ]
}

// Reference the existing Service Bus namespace to scope role assignments correctly
resource serviceBusNamespace 'Microsoft.ServiceBus/namespaces@2021-11-01' existing = {
  name: last(split(serviceBusNamespaceId, '/'))
}

// Grant Event Grid user assigned identity permission to send messages to Service Bus
resource eventGridServiceBusDataSenderRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(serviceBusNamespaceId, eventGridIdentity.id, roles.serviceBusDataSender, 'eventgrid-servicebus-sender')
  scope: serviceBusNamespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataSender)
    principalId: eventGridIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

// Grant Event Grid user assigned identity permission to receive messages from Service Bus (required for delivery confirmation)
resource eventGridServiceBusDataReceiverRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(serviceBusNamespaceId, eventGridIdentity.id, roles.serviceBusDataReceiver, 'eventgrid-servicebus-receiver')
  scope: serviceBusNamespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataReceiver)
    principalId: eventGridIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

@description('The Event Grid System Topic resource ID')
output systemTopicId string = blobSystemTopic.id

@description('The Event Grid subscription resource ID')
output eventSubscriptionId string = blobEventSubscription.id

@description('The Event Grid user assigned identity principal ID')
output eventGridIdentityPrincipalId string = eventGridIdentity.properties.principalId

@description('The Event Grid user assigned identity resource ID')
output eventGridIdentityId string = eventGridIdentity.id

@description('The Event Grid user assigned identity client ID')
output eventGridIdentityClientId string = eventGridIdentity.properties.clientId
