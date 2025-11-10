// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('The name for the AKS cluster')
param aksClusterName string

@description('The location for all resources')
param location string = resourceGroup().location

@description('The container registry name for pulling images')
param containerRegistryName string

@description('The resource ID of the centralized Log Analytics workspace')
param logAnalyticsWorkspaceId string

@description('The Kubernetes version for AKS')
param kubernetesVersion string = '1.34.0'

// Internal implementation details
var aksNodePoolName = 'system'
var cpuNodePoolName = 'cpupool'
var gpuNodePoolName = 'gpupool'

resource aksCluster 'Microsoft.ContainerService/managedClusters@2025-03-01' = {
  name: aksClusterName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    kubernetesVersion: kubernetesVersion
    dnsPrefix: aksClusterName
    oidcIssuerProfile: {
      enabled: true
    }
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    agentPoolProfiles: [
      {
        name: aksNodePoolName
        count: 2
        vmSize: 'Standard_D4s_v3'
        osType: 'Linux'
        mode: 'System'
        osSKU: 'Ubuntu'
        osDiskType: 'Managed'
        enableAutoScaling: true
        minCount: 1
        maxCount: 5
      }
    ]
    networkProfile: {
      networkPlugin: 'azure'
      networkPolicy: 'azure'
    }
    addonProfiles: {
      omsAgent: {
        enabled: true
        config: {
          logAnalyticsWorkspaceResourceID: logAnalyticsWorkspaceId
        }
      }
      azureKeyvaultSecretsProvider: {
        enabled: true
        config: {
          enableSecretRotation: 'true'
          rotationPollInterval: '2m'
        }
      }
    }
    autoUpgradeProfile: {
      upgradeChannel: 'patch'
    }
    azureMonitorProfile: {
      metrics: {
        enabled: true
      }
    }
    workloadAutoScalerProfile: {
      keda: {
        enabled: false
      }
    }
    storageProfile: {
      blobCSIDriver: {
        enabled: true
      }
    }
  }
}

// CPU node pool for general workloads (Qdrant, Document Processor, etc.)
resource cpuNodePool 'Microsoft.ContainerService/managedClusters/agentPools@2024-07-01' = {
  parent: aksCluster
  name: cpuNodePoolName
  properties: {
    count: 2
    vmSize: 'Standard_D4s_v3' // 4 vCPU, 16GB RAM
    osType: 'Linux'
    mode: 'User'
    osSKU: 'Ubuntu'
    osDiskType: 'Managed'
    enableAutoScaling: true
    minCount: 0
    maxCount: 10
    nodeLabels: {
      agentpool: cpuNodePoolName
      workload: 'cpu'
      compute: 'azureml'
    }
    nodeTaints: []
  }
}

// // GPU node pool for ML inference and training workloads
// resource gpuNodePool 'Microsoft.ContainerService/managedClusters/agentPools@2024-07-01' = {
//   parent: aksCluster
//   name: gpuNodePoolName
//   properties: {
//     count: 1
//     vmSize: 'standard_nv6ads_a10_v5'
//     osType: 'Linux'
//     mode: 'User'
//     osSKU: 'Ubuntu'
//     osDiskType: 'Managed'
//     enableAutoScaling: true
//     minCount: 0
//     maxCount: 5
//     nodeLabels: {
//       agentpool: gpuNodePoolName
//       workload: 'gpu'
//       compute: 'azureml'
//       sku: 'gpu'
//     }
//     nodeTaints: [
//       'sku=gpu:NoSchedule' // Ensure only GPU workloads get scheduled here
//     ]
//   }
// }

@description('The name of the AKS cluster')
output aksClusterName string = aksCluster.name

@description('The resource ID of the AKS cluster')
output aksClusterId string = aksCluster.id

@description('The FQDN of the AKS cluster')
output aksClusterFqdn string = aksCluster.properties.fqdn

@description('The container registry login server for Helm chart values')
output containerRegistryLoginServer string = '${containerRegistryName}.azurecr.io'

@description('The OIDC issuer URL for workload identity')
output oidcIssuerUrl string = aksCluster.properties.oidcIssuerProfile.issuerURL

@description('The principal ID of the kubelet identity for ACR pull access')
output kubeletIdentityObjectId string = aksCluster.properties.identityProfile.kubeletidentity.objectId

@description('The client ID of the kubelet identity for ACR pull access')
output kubeletIdentityClientId string = aksCluster.properties.identityProfile.kubeletidentity.clientId

@description('The principal ID of the AKS control plane system-assigned identity')
output aksIdentityPrincipalId string = aksCluster.identity.principalId
