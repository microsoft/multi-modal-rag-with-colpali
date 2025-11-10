// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.
@description('The name of the workload identity (user assigned managed identity)')
param aksIdentityName string

@description('The OIDC issuer URL from AKS cluster')
param oidcIssuerUrl string

@description('The Kubernetes namespace for the service account')
param namespace string = 'default'

@description('The Kubernetes service account name')
param serviceAccountName string = 'document-processor'

resource workloadIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: aksIdentityName
}

resource federatedIdentityCredential 'Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials@2023-01-31' = {
  parent: workloadIdentity
  name: 'workload-federated-identity'
  properties: {
    issuer: oidcIssuerUrl
    subject: 'system:serviceaccount:${namespace}:${serviceAccountName}'
    audiences: [
      'api://AzureADTokenExchange'
    ]
  }
}

@description('The name of the federated identity credential')
output federatedIdentityName string = federatedIdentityCredential.name
