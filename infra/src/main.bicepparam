using 'main.bicep'

param baseName = 'colqwen'
param location = 'uksouth'
param deployRoleAssignments = true
param deployContainerApps = false
param acrSku = 'Basic'
param acrAdminUserEnabled = true
param amlSku = 'Basic'
param amlEmbeddingEndpointType = 'Standard_NC24ads_A100_v4'
param amlEmbeddingEndpointCount = 1
param jobInstanceType = 'Standard_DS3_v2'
param jobInstanceCount = 1
