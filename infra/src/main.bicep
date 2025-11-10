// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('Base name for all resources')
param baseName string

@description('The location for all resources')
param location string = resourceGroup().location

@description('Flag to control whether to deploy role assignments (set to false if they already exist)')
param deployRoleAssignments bool = false

@description('The SKU name for the Azure Container Registry')
@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param acrSku string = 'Basic'

@description('The optional object ID of the user to assign to the compute instance (if empty, will be auto-assigned)')
param userObjectId string = ''

var acrName = replace('cr${baseName}', '-', '')
var aiFoundryName = replace('aif-${baseName}', '-', '')
var dataStorageAccountName = replace('stdata${baseName}', '-', '')
var serviceBusNamespaceName = 'sbns-${baseName}'
var logAnalyticsWorkspaceName = 'logs-${baseName}'
var applicationInsightsName = 'appi-${baseName}'
var workloadIdentityName = 'id-aks-workload-${baseName}'
var aksClusterName = 'aks-${baseName}'

module acrModule 'modules/containerRegistry.bicep' = {
  name: 'acrDeployment'
  params: {
    acrName: acrName
    location: location
    acrSku: acrSku
  }
}

module monitoringModule 'modules/monitoring.bicep' = {
  name: 'monitoringDeployment'
  params: {
    logAnalyticsWorkspaceName: logAnalyticsWorkspaceName
    applicationInsightsName: applicationInsightsName
    location: location
  }
}

module dataStorageModule 'modules/dataStorage.bicep' = {
  name: 'dataStorageDeployment'
  params: {
    dataStorageAccountName: dataStorageAccountName
    location: location
  }
}
module serviceBusModule 'modules/serviceBus.bicep' = {
  name: 'serviceBusDeployment'
  params: {
    serviceBusNamespaceName: serviceBusNamespaceName
    location: location
    serviceBusSku: 'Standard'
  }
}
module eventGridModule 'modules/eventGrid.bicep' = {
  name: 'eventGridDeployment'
  params: {
    baseName: baseName
    location: location
    dataStorageAccountId: dataStorageModule.outputs.dataStorageAccountId
    dataStorageAccountName: dataStorageModule.outputs.dataStorageAccountName
    serviceBusNamespaceId: serviceBusModule.outputs.serviceBusNamespaceId
    serviceBusQueueName: serviceBusModule.outputs.documentProcessingQueueName
  }
}

module aiFoundryModule 'modules/aiFoundry.bicep' = {
  name: 'aiFoundryDeployment'
  params: {
    aiFoundryName: aiFoundryName
    location: location
  }
}

// User-assigned identity for workload identity (pods accessing Azure resources)
resource workloadIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: workloadIdentityName
  location: location
}

module aksModule 'modules/aks.bicep' = {
  name: 'aksDeployment'
  params: {
    aksClusterName: aksClusterName
    location: location
    containerRegistryName: acrName
    logAnalyticsWorkspaceId: monitoringModule.outputs.logAnalyticsWorkspaceId
  }
}

module aksFederatedIdentityModule 'modules/aksFederatedIdentity.bicep' = {
  name: 'aksFederatedIdentityDeployment'
  params: {
    aksIdentityName: workloadIdentity.name
    oidcIssuerUrl: aksModule.outputs.oidcIssuerUrl
    namespace: 'colpali-stack'
    serviceAccountName: 'colpali-stack-sa'
  }
}

module keyVaultModule 'modules/keyVault.bicep' = {
  name: 'keyVaultDeployment'
  params: {
    baseName: baseName
    location: location
    applicationInsightsConnectionString: monitoringModule.outputs.applicationInsightsConnectionString
  }
}
module roleAssignmentsModule 'modules/roleAssignments.bicep' = {
  name: 'roleAssignmentsDeployment'
  params: {
    dataStorageAccountId: dataStorageModule.outputs.dataStorageAccountId
    aiFoundryServiceId: aiFoundryModule.outputs.aiFoundryId
    containerRegistryId: acrModule.outputs.acrId
    userObjectId: userObjectId
    aksKubeletIdentityPrincipalId: aksModule.outputs.kubeletIdentityObjectId
    aksWorkloadPrincipalId: workloadIdentity.properties.principalId
    serviceBusNamespaceId: serviceBusModule.outputs.serviceBusNamespaceId
    keyVaultId: keyVaultModule.outputs.keyVaultId
    deployRoleAssignments: deployRoleAssignments
  }
}
@description('The name of the resource group')
output resourceGroup string = resourceGroup().name

@description('The subscription ID')
output subscriptionId string = subscription().subscriptionId

@description('The tenant ID')
output tenantId string = tenant().tenantId

@description('The login server for the Azure Container Registry')
output acrLoginServer string = acrModule.outputs.acrLoginServer

@description('The name of the Azure Container Registry')
output acrName string = acrModule.outputs.acrName

@description('The name of the data storage account')
output dataStorageAccountName string = dataStorageModule.outputs.dataStorageAccountName

@description('The name of the documents container')
output dataStorageContainerName string = dataStorageModule.outputs.documentsContainerName

@description('The name of the AI Foundry service')
output aiFoundryName string = aiFoundryModule.outputs.aiFoundryName

@description('The endpoint URL of the AI Foundry service')
output aiFoundryEndpoint string = aiFoundryModule.outputs.aiFoundryEndpoint

@description('The principal ID of the AI Foundry managed identity')
output aiFoundryPrincipalId string = aiFoundryModule.outputs.aiFoundryPrincipalId

@description('The name of the AI Project')
output aiProjectName string = aiFoundryModule.outputs.aiProjectName

// @description('The name of the deployed model')
// output modelName string = aiFoundryModule.outputs.modelDeploymentName

@description('The AKS cluster name')
output aksClusterName string = aksModule.outputs.aksClusterName

@description('The AKS cluster resource ID')
output aksClusterId string = aksModule.outputs.aksClusterId

@description('The AKS cluster FQDN')
output aksClusterFqdn string = aksModule.outputs.aksClusterFqdn

@description('The workload identity client ID for AKS pods')
output workloadIdentityClientId string = workloadIdentity.properties.clientId
@description('The kubelet identity client ID for AKS node access')
output aksKubeletIdentityClientId string = aksModule.outputs.kubeletIdentityClientId

@description('The Service Bus namespace name')
output serviceBusNamespaceName string = serviceBusModule.outputs.serviceBusNamespaceName

@description('The Service Bus queue name for document processing')
output serviceBusQueueName string = serviceBusModule.outputs.documentProcessingQueueName

@description('Event Grid System Topic ID')
output eventGridSystemTopicId string = eventGridModule.outputs.systemTopicId

@description('Event Grid Event Subscription ID')
output eventGridEventSubscriptionId string = eventGridModule.outputs.eventSubscriptionId

@description('The Key Vault name')
output keyVaultName string = keyVaultModule.outputs.keyVaultName

@description('The Key Vault URI')
output keyVaultUri string = keyVaultModule.outputs.keyVaultUri

@description('The Qdrant API key secret name')
output qdrantApiKeySecretName string = keyVaultModule.outputs.qdrantApiKeySecretName

@description('The Qdrant read-only API key secret name')
output qdrantReadOnlyApiKeySecretName string = keyVaultModule.outputs.qdrantReadOnlyApiKeySecretName

@description('The Application Insights connection string secret name')
output applicationInsightsConnectionStringSecretName string = keyVaultModule.outputs.applicationInsightsConnectionStringSecretName
