@description('The resource ID of the Azure Machine Learning workspace')
param amlWorkspaceId string

@description('The name of the compute instance')
param computeInstanceName string

@description('The VM size for the compute instance')
param computeInstanceVmSize string

@description('Enable idle shutdown for the compute instance')
param idleShutdownEnabled bool = true

@description('The number of minutes of inactivity after which the compute instance should shut down (between 5 and 10080)')
@minValue(5)
@maxValue(10080)
param idleShutdownInMinutes int = 60

@description('Enable scheduled shutdown for the compute instance at a specific time')
param scheduledShutdownEnabled bool = false

@description('The time (in UTC) when the compute instance should automatically shut down if scheduled shutdown is enabled')
param scheduledShutdownTime string = '20:00' // 8 PM UTC

@description('The description for the compute instance')
param computeInstanceDescription string = 'GPU compute instance for ONNX model conversion and quantization'

@description('The login server for the Azure Container Registry')
param acrLoginServer string = ''

@description('The name of the Azure Container Registry')
param acrName string = ''

@description('The tenant ID for the Azure subscription')
param tenantId string = subscription().tenantId

@description('The optional object ID of the user to assign to the compute instance (if empty, will be auto-assigned)')
param userObjectId string = ''

@description('The location for the compute instance')
param location string = resourceGroup().location

@description('The resource ID of the pre-created user-assigned managed identity')
param computeIdentityId string

// Get the existing AML workspace
resource amlWorkspace 'Microsoft.MachineLearningServices/workspaces@2025-07-01-preview' existing = {
  name: split(amlWorkspaceId, '/')[8]
}

// Create the main compute instance using the pre-created identity
resource computeInstance 'Microsoft.MachineLearningServices/workspaces/computes@2025-07-01-preview' = {
  parent: amlWorkspace
  name: computeInstanceName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${computeIdentityId}': {}
    }
  }
  properties: {
    computeType: 'ComputeInstance'
    properties: {
      vmSize: computeInstanceVmSize
      personalComputeInstanceSettings: {
        assignedUser: {
          objectId: empty(userObjectId) ? '' : userObjectId
          tenantId: tenantId
        }
      }
      enableSSO: false
      enableNodePublicIp: true
      sshSettings: {
        sshPublicAccess: 'Enabled'
        adminPublicKey: ''
      }
      schedules: scheduledShutdownEnabled ? {
        computeStartStop: [
          {
            action: 'Stop'
            triggerType: 'Cron'
            status: 'Enabled'
            cron: {
              expression: '0 ${substring(scheduledShutdownTime, 3, 2)} ${substring(scheduledShutdownTime, 0, 2)} * * *'
              timeZone: 'UTC'
            }
          }
        ]
      } : {}
      idleTimeBeforeShutdown: idleShutdownEnabled ? 'PT${idleShutdownInMinutes}M' : null
      // Add ACR environment variables
      setupScripts: {
        scripts: {
          creationScript: {
            scriptSource: 'inline'
            scriptData: base64('''
#!/bin/bash
echo "Setting up environment variables for ACR access"
echo "export ACR_NAME=$1" >> /home/azureuser/.bashrc
echo "export ACR_LOGIN_SERVER=$2" >> /home/azureuser/.bashrc
echo "export AZURE_ML_WORKSPACE_NAME=$3" >> /home/azureuser/.bashrc
# Create directory if it doesn't exist
mkdir -p /mnt/cache/huggingface
echo "export HF_HOME=/mnt/cache/huggingface" >> /home/azureuser/.bashrc
echo "Environment variables for ACR configured"
''')
            scriptArguments: '${acrName} ${acrLoginServer} ${split(amlWorkspaceId, '/')[8]}'
          }
        }
      }
    }
    description: computeInstanceDescription
  }
}

// ------------------------------------------------------------
// OUTPUTS
// ------------------------------------------------------------
@description('The name of the compute instance')
output computeInstanceName string = computeInstance.name

@description('The ID of the compute instance')
output computeInstanceId string = computeInstance.id
