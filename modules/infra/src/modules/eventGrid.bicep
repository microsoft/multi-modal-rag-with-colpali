// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
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

var systemTopicName = 'egst-${dataStorageAccountName}-blobs'
var eventGridIdentityName = 'id-${systemTopicName}'

// Create User Assigned Identity for Event Grid. Needed to authenticate to Service Bus which needs to be authorised before the subscription can be created.
module eventGridIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'eventGridIdentity'
  params: {
    name: eventGridIdentityName
    location: location
    enableTelemetry: false
  }
}

module blobSystemTopic 'br/public:avm/res/event-grid/system-topic:0.7.0' = {
  name: 'blobSystemTopic'
  params: {
    name: systemTopicName
    location: location
    enableTelemetry: false
    managedIdentities: {
      userAssignedResourceIds: [
        eventGridIdentity.outputs.resourceId
      ]
    }
    source: dataStorageAccountId
    topicType: 'Microsoft.Storage.StorageAccounts'
    eventSubscriptions: [
      {
        name: 'egs-${baseName}-document-processing'
        deliveryWithResourceIdentity: {
          destination: {
            endpointType: 'ServiceBusQueue'
            properties: {
              resourceId: '${serviceBusNamespaceId}/queues/${serviceBusQueueName}'
            }
          }
          identity: {
            type: 'UserAssigned'
            userAssignedIdentity: eventGridIdentity.outputs.resourceId
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
    ]
  }
  dependsOn: [
    eventGridServiceBusDataSenderRole
    eventGridServiceBusDataReceiverRole
  ]
}

// Grant Event Grid user assigned identity permission to send messages to Service Bus
module eventGridServiceBusDataSenderRole 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = {
  name: 'eventGridServiceBusDataSenderRole'
  params: {
    name: guid(serviceBusNamespaceId, eventGridIdentity.outputs.resourceId, roles.serviceBusDataSender, 'eventgrid-servicebus-sender')
    resourceId: serviceBusNamespaceId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataSender)
    principalId: eventGridIdentity.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// Grant Event Grid user assigned identity permission to receive messages from Service Bus (required for delivery confirmation)
module eventGridServiceBusDataReceiverRole 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = {
  name: 'eventGridServiceBusDataReceiverRole'
  params: {
    name: guid(serviceBusNamespaceId, eventGridIdentity.outputs.resourceId, roles.serviceBusDataReceiver, 'eventgrid-servicebus-receiver')
    resourceId: serviceBusNamespaceId
    enableTelemetry: false
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.serviceBusDataReceiver)
    principalId: eventGridIdentity.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

@description('The Event Grid System Topic resource ID')
output systemTopicId string = blobSystemTopic.outputs.resourceId

@description('The Event Grid subscription resource ID')
output eventSubscriptionId string = '${blobSystemTopic.outputs.resourceId}/eventSubscriptions/egs-${baseName}-document-processing'

@description('The Event Grid user assigned identity principal ID')
output eventGridIdentityPrincipalId string = eventGridIdentity.outputs.principalId

@description('The Event Grid user assigned identity resource ID')
output eventGridIdentityId string = eventGridIdentity.outputs.resourceId

@description('The Event Grid user assigned identity client ID')
output eventGridIdentityClientId string = eventGridIdentity.outputs.clientId
