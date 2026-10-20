// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('The name of the workload identity (user assigned managed identity)')
param aksIdentityName string

@description('The location for the workload identity')
param location string = resourceGroup().location

@description('The OIDC issuer URL from AKS cluster')
param oidcIssuerUrl string

@description('The Kubernetes namespace for the service account')
param namespace string = 'default'

@description('The Kubernetes service account name')
param serviceAccountName string = 'document-processor'

module workloadIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'workloadIdentity'
  params: {
    name: aksIdentityName
    location: location
    enableTelemetry: false
    federatedIdentityCredentials: [
      {
        name: 'workload-federated-identity'
        issuer: oidcIssuerUrl
        subject: 'system:serviceaccount:${namespace}:${serviceAccountName}'
        audiences: [
          'api://AzureADTokenExchange'
        ]
      }
    ]
  }
}

@description('The name of the federated identity credential')
output federatedIdentityName string = 'workload-federated-identity'

@description('The workload identity client ID')
output clientId string = workloadIdentity.outputs.clientId

@description('The workload identity principal ID')
output principalId string = workloadIdentity.outputs.principalId
