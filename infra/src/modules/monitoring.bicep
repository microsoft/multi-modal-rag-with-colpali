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

// Create the Log Analytics workspace that we own and control
resource logAnalyticsWorkspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsWorkspaceName
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: retentionInDays
    features: {
      searchVersion: 1
      legacy: 0
      enableLogAccessUsingOnlyResourcePermissions: true
    }
    workspaceCapping: {
      dailyQuotaGb: 10 // Set a reasonable daily quota to control costs
    }
  }
}

// Create Application Insights that uses our Log Analytics workspace
resource applicationInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: applicationInsightsName
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalyticsWorkspace.id
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

@description('The resource ID of the Log Analytics workspace')
output logAnalyticsWorkspaceId string = logAnalyticsWorkspace.id

@description('The name of the Log Analytics workspace')
output logAnalyticsWorkspaceName string = logAnalyticsWorkspace.name

@description('The resource ID of Application Insights')
output applicationInsightsId string = applicationInsights.id

@description('The name of Application Insights')
output applicationInsightsName string = applicationInsights.name

@description('The instrumentation key for Application Insights')
output applicationInsightsInstrumentationKey string = applicationInsights.properties.InstrumentationKey

@description('The connection string for Application Insights')
output applicationInsightsConnectionString string = applicationInsights.properties.ConnectionString
