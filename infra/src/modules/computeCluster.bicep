@description('The name of the Azure Machine Learning workspace')
param workspaceName string

@description('The name of the compute cluster')
param clusterName string

@description('The location for the compute cluster')
param location string

@description('The VM size for the compute cluster')
param vmSize string = 'Standard_NC24ads_A100_v4'

@description('Minimum number of nodes')
@minValue(0)
param minNodeCount int = 0

@description('Maximum number of nodes')
@minValue(1)
param maxNodeCount int = 4

@description('Idle seconds before scale down')
@minValue(0)
param idleSecondsBeforeScaledown int = 1800

resource workspace 'Microsoft.MachineLearningServices/workspaces@2024-04-01' existing = {
  name: workspaceName
}

resource computeIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${clusterName}' // Managed Identity: CAF standard 'id'
  location: location
}

resource computeCluster 'Microsoft.MachineLearningServices/workspaces/computes@2024-04-01' = {
  parent: workspace
  name: clusterName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${computeIdentity.id}': {}
    }
  }
  properties: {
    computeType: 'AmlCompute'
    properties: {
      vmSize: vmSize
      vmPriority: 'Dedicated'
      scaleSettings: {
        minNodeCount: minNodeCount
        maxNodeCount: maxNodeCount
        nodeIdleTimeBeforeScaleDown: 'PT${idleSecondsBeforeScaledown}S'
      }
      enableNodePublicIp: true
      isolatedNetwork: false
      osType: 'Linux'
      remoteLoginPortPublicAccess: 'Enabled'
    }
  }
}

output clusterName string = computeCluster.name
output clusterId string = computeCluster.id
output clusterPrincipalId string = computeIdentity.properties.principalId
