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

@description('Optional Kubernetes version for an explicit cluster and node-pool upgrade. Leave null to avoid reapplying a stale patch version after automatic upgrades.')
param kubernetesVersion string?

module aksCluster 'br/public:avm/res/container-service/managed-cluster:0.14.0' = {
  name: 'aksCluster'
  params: {
    name: aksClusterName
    location: location
    enableTelemetry: false
    managedIdentities: {
      systemAssigned: true
    }
    skuTier: 'Free'
    nodeResourceGroup: 'MC_${resourceGroup().name}_${aksClusterName}_${location}'
    publicNetworkAccess: 'Enabled'
    disableLocalAccounts: false
    kubernetesVersion: kubernetesVersion
    dnsPrefix: aksClusterName
    enableOidcIssuerProfile: true
    securityProfile: {
      workloadIdentity: {
        enabled: true
      }
    }
    primaryAgentPoolProfiles: [
      {
        name: 'systempool'
        availabilityZones: []
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
    agentPools: [
      {
        name: 'cpupool'
        availabilityZones: []
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
      {
        name: 'gpupool'
        availabilityZones: []
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
        spotMaxPrice: -1
        nodeLabels: {
          agentpool: 'gpupool'
          workload: 'gpu'
          sku: 'gpu'
        }
        nodeTaints: [
          'sku=gpu:NoSchedule'
        ]
      }
    ]
    networkPlugin: 'azure'
    networkPolicy: 'azure'
    omsAgentEnabled: false
    azurePolicyEnabled: false
    enableKeyvaultSecretsProvider: true
    enableSecretRotation: true
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
    costAnalysisEnabled: false
    workloadAutoScalerProfile: {
      keda: {
        enabled: false
      }
    }
    enableStorageProfileBlobCSIDriver: true
    enableStorageProfileDiskCSIDriver: true
    enableStorageProfileFileCSIDriver: true
    enableStorageProfileSnapshotController: true
    diagnosticSettings: [
      {
        name: replace('diag-${aksClusterName}', 'aks-', '')
        workspaceResourceId: logAnalyticsWorkspaceId
        logAnalyticsDestinationType: 'AzureDiagnostics'
        logCategoriesAndGroups: [
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
        metricCategories: [
          {
            category: 'AllMetrics'
            enabled: true
          }
        ]
      }
    ]
  }
}

// Data Collection Rule for Prometheus metrics (Azure Monitor managed Prometheus)
module prometheusDataCollectionRule 'br/public:avm/res/insights/data-collection-rule:0.11.0' = {
  name: 'prometheusDataCollectionRule'
  params: {
    name: replace('dcr-${aksClusterName}', 'aks-', '')
    location: location
    enableTelemetry: false
    dataCollectionRuleProperties: {
      kind: 'Linux'
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
}

resource aksClusterResource 'Microsoft.ContainerService/managedClusters@2025-03-01' existing = {
  name: aksClusterName
}

// The DCR AVM does not expose associations to arbitrary resources.
resource prometheusDataCollectionRuleAssociation 'Microsoft.Insights/dataCollectionRuleAssociations@2023-03-11' = {
  name: replace('dcra-${aksClusterName}', 'aks-', '')
  scope: aksClusterResource
  properties: {
    dataCollectionRuleId: prometheusDataCollectionRule.outputs.resourceId
  }
  dependsOn: [
    aksCluster
  ]
}

@description('The name of the AKS cluster')
output aksClusterName string = aksCluster.outputs.name

@description('The resource ID of the AKS cluster')
output aksClusterId string = aksCluster.outputs.resourceId

@description('The FQDN of the AKS cluster')
output aksClusterFqdn string = aksCluster.outputs.controlPlaneFQDN

@description('The container registry login server for Helm chart values')
output containerRegistryLoginServer string = '${containerRegistryName}.azurecr.io'

@description('The OIDC issuer URL for workload identity')
output oidcIssuerUrl string = aksCluster.outputs.oidcIssuerUrl!

@description('The principal ID of the kubelet identity for ACR pull access')
output kubeletIdentityObjectId string = aksCluster.outputs.kubeletIdentityObjectId!

@description('The client ID of the kubelet identity for ACR pull access')
output kubeletIdentityClientId string = aksCluster.outputs.kubeletIdentityClientId!

@description('The principal ID of the AKS control plane system-assigned identity')
output aksIdentityPrincipalId string = aksCluster.outputs.systemAssignedMIPrincipalId!
