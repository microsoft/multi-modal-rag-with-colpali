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

@description('The resource ID of the Azure Monitor workspace for Prometheus')
param azureMonitorWorkspaceId string

@description('The Kubernetes version for AKS')
param kubernetesVersion string = '1.34.0'

// Internal implementation details
var aksNodePoolName = 'system'

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
        enabled: false
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
    autoScalerProfile: {
      'scale-down-delay-after-add': '5m'
      'scale-down-delay-after-delete': '10s'
      'scale-down-delay-after-failure': '3m'
      'scale-down-unneeded-time': '5m'
      'scale-down-unready-time': '20m'
      'scan-interval': '10s'
    }
    azureMonitorProfile: {
      metrics: {
        enabled: true
        kubeStateMetrics: {
          metricLabelsAllowlist: ''
          metricAnnotationsAllowList: ''
        }
      }
    }
    metricsProfile: {
      costAnalysis: {
        enabled: false
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
  name: 'cpupool'
  properties: {
    count: 0
    vmSize: 'Standard_D8s_v3'
    osType: 'Linux'
    mode: 'User'
    osSKU: 'Ubuntu'
    osDiskType: 'Managed'
    enableAutoScaling: true
    minCount: 0
    maxCount: 5
    nodeLabels: {
      agentpool: 'cpupool'
      workload: 'cpu'
    }
    nodeTaints: []
  }
}

// GPU node pool for inference workloads (spot instances)
resource gpuInferenceNodePool 'Microsoft.ContainerService/managedClusters/agentPools@2024-07-01' = {
  parent: aksCluster
  name: 'gpuinference'
  properties: {
    count: 0
    vmSize: 'standard_nv18ads_a10_v5'
    osType: 'Linux'
    mode: 'User'
    osSKU: 'Ubuntu'
    osDiskType: 'Managed'
    enableAutoScaling: true
    minCount: 0
    maxCount: 3
    scaleSetPriority: 'Spot'
    scaleSetEvictionPolicy: 'Delete'
    spotMaxPrice: -1 // Pay up to on-demand price
    nodeLabels: {
      agentpool: 'gpuinference'
      workload: 'gpu'
      sku: 'gpu'
    }
    nodeTaints: [
      'sku=gpu:NoSchedule' // Ensure only GPU workloads get scheduled here
    ]
  }
}

// Data Collection Rule for Prometheus metrics (Azure Monitor managed Prometheus)
resource prometheusDataCollectionRule 'Microsoft.Insights/dataCollectionRules@2023-03-11' = {
  name: replace('dcr-${aksClusterName}', 'aks-', '')
  location: location
  kind: 'Linux'
  properties: {
    dataSources: {
      prometheusForwarder: [
        {
          name: 'PrometheusDataSource'
          streams: [
            'Microsoft-PrometheusMetrics'
          ]
          labelIncludeFilter: {}
        }
      ]
    }
    destinations: {
      monitoringAccounts: [
        {
          accountResourceId: azureMonitorWorkspaceId
          name: 'MonitoringAccount1'
        }
      ]
    }
    dataFlows: [
      {
        streams: [
          'Microsoft-PrometheusMetrics'
        ]
        destinations: [
          'MonitoringAccount1'
        ]
      }
    ]
  }
}

// Data Collection Rule Association for Prometheus
resource prometheusDataCollectionRuleAssociation 'Microsoft.Insights/dataCollectionRuleAssociations@2023-03-11' = {
  name: replace('dcra-${aksClusterName}', 'aks-', '')
  scope: aksCluster
  properties: {
    dataCollectionRuleId: prometheusDataCollectionRule.id
  }
}

// Diagnostic settings for AKS control plane logs
resource aksControlPlaneDiagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = {
  name: replace('diag-${aksClusterName}', 'aks-', '')
  scope: aksCluster
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    logs: [
      {
        category: 'kube-apiserver'
        enabled: true
      }
      {
        category: 'kube-audit'
        enabled: false
      }
      {
        category: 'kube-audit-admin'
        enabled: false
      }
      {
        category: 'kube-controller-manager'
        enabled: true
      }
      {
        category: 'kube-scheduler'
        enabled: true
      }
      {
        category: 'cluster-autoscaler'
        enabled: true
      }
      {
        category: 'cloud-controller-manager'
        enabled: true
      }
      {
        category: 'guard'
        enabled: true
      }
      {
        category: 'csi-azuredisk-controller'
        enabled: true
      }
      {
        category: 'csi-azurefile-controller'
        enabled: true
      }
      {
        category: 'csi-snapshot-controller'
        enabled: true
      }
    ]
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

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
