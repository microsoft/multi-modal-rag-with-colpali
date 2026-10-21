// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('The name for the Log Analytics workspace')
param logAnalyticsWorkspaceName string

@description('The name for the Application Insights resource')
param applicationInsightsName string

@description('The location for all resources')
param location string = resourceGroup().location

@description('The retention period in days for Log Analytics workspace')
param retentionInDays int = 30

@description('Tags to apply to all resources')
param tags object = {}

module logAnalyticsWorkspace 'br/public:avm/res/operational-insights/workspace:0.16.1' = {
  name: 'logAnalyticsWorkspace'
  params: {
    name: logAnalyticsWorkspaceName
    location: location
    tags: tags
    enableTelemetry: false
    skuName: 'PerGB2018'
    dataRetention: retentionInDays
    dailyQuotaGb: '2'
    forceCmkForQuery: false
    features: {
      enableLogAccessUsingOnlyResourcePermissions: true
      disableLocalAuth: false
    }
  }
}

// No published AVM resource module currently covers Microsoft.Monitor/accounts.
resource azureMonitorWorkspace 'Microsoft.Monitor/accounts@2023-04-03' = {
  name: replace('amw-${logAnalyticsWorkspaceName}', 'logs-', '')
  location: location
  tags: tags
  properties: {}
}

module applicationInsights 'br/public:avm/res/insights/component:0.8.0' = {
  name: 'applicationInsights'
  params: {
    name: applicationInsightsName
    location: location
    tags: tags
    enableTelemetry: false
    kind: 'web'
    applicationType: 'web'
    workspaceResourceId: logAnalyticsWorkspace.outputs.resourceId
    ingestionMode: 'LogAnalytics'
    disableIpMasking: false
    retentionInDays: 90
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

@description('The resource ID of the Log Analytics workspace')
output logAnalyticsWorkspaceId string = logAnalyticsWorkspace.outputs.resourceId

@description('The name of the Log Analytics workspace')
output logAnalyticsWorkspaceName string = logAnalyticsWorkspace.outputs.name

@description('The resource ID of Application Insights')
output applicationInsightsId string = applicationInsights.outputs.resourceId

@description('The name of Application Insights')
output applicationInsightsName string = applicationInsights.outputs.name

@description('The instrumentation key for Application Insights')
output applicationInsightsInstrumentationKey string = applicationInsights.outputs.instrumentationKey

@description('The connection string for Application Insights')
output applicationInsightsConnectionString string = applicationInsights.outputs.connectionString

@description('The resource ID of the Azure Monitor workspace')
output azureMonitorWorkspaceId string = azureMonitorWorkspace.id

@description('The default ingestion endpoint for Azure Monitor workspace')
output azureMonitorWorkspaceDefaultIngestionEndpoint string = azureMonitorWorkspace.properties.defaultIngestionSettings.dataCollectionEndpointResourceId
