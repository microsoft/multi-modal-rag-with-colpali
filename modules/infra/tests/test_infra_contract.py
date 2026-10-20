# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Offline regression checks for the compiled infrastructure contract."""

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

INFRA = Path(__file__).resolve().parents[1]


def resources(template):
    entries = template.get("resources", [])
    return list(entries.values()) if isinstance(entries, dict) else entries


def resource(template, name):
    return next(item for item in resources(template) if item["name"] == name)


def parameters(module):
    return {
        key: item["value"] for key, item in module["properties"]["parameters"].items()
    }


class InfrastructureContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        az = shutil.which("az")
        if not az:
            raise RuntimeError("Azure CLI with Bicep is required to run these tests.")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "main.json"
            subprocess.run(
                [
                    az,
                    "bicep",
                    "build",
                    "--file",
                    str(INFRA / "src" / "main.bicep"),
                    "--outfile",
                    str(output),
                    "--only-show-errors",
                ],
                check=True,
            )
            cls.template = json.loads(output.read_text(encoding="utf-8"))

    def wrapper(self, name):
        return resource(self.template, name)["properties"]["template"]

    def avm(self, wrapper, name):
        return parameters(resource(self.wrapper(wrapper), name))

    def assert_subset(self, actual, expected):
        for key, value in expected.items():
            with self.subTest(key=key):
                self.assertEqual(actual[key], value)

    def test_public_parameters_and_outputs(self):
        self.assertEqual(
            self.template["variables"],
            {
                "acrName": "[replace(format('cr{0}', parameters('baseName')), '-', '')]",
                "aiFoundryName": "[replace(format('aif-{0}', parameters('baseName')), '-', '')]",
                "dataStorageAccountName": "[replace(format('stdata{0}', parameters('baseName')), '-', '')]",
                "serviceBusNamespaceName": "[format('sbns-{0}', parameters('baseName'))]",
                "logAnalyticsWorkspaceName": "[format('logs-{0}', parameters('baseName'))]",
                "applicationInsightsName": "[format('appi-{0}', parameters('baseName'))]",
                "workloadIdentityName": "[format('id-aks-workload-{0}', parameters('baseName'))]",
                "aksClusterName": "[format('aks-{0}', parameters('baseName'))]",
            },
        )
        self.assertEqual(
            set(self.template["parameters"]),
            {"baseName", "location", "deployRoleAssignments", "acrSku", "userObjectId"},
        )
        for name, default in {
            "location": "[resourceGroup().location]",
            "deployRoleAssignments": False,
            "acrSku": "Basic",
            "userObjectId": "",
        }.items():
            self.assertEqual(self.template["parameters"][name]["defaultValue"], default)
        self.assertNotIn("defaultValue", self.template["parameters"]["baseName"])
        self.assertEqual(
            self.template["parameters"]["acrSku"]["allowedValues"],
            ["Basic", "Standard", "Premium"],
        )
        outputs = {
            "acrDeployment": {"acrLoginServer": "acrLoginServer", "acrName": "acrName"},
            "dataStorageDeployment": {
                "dataStorageAccountName": "dataStorageAccountName",
                "dataStorageContainerName": "documentsContainerName",
            },
            "aiFoundryDeployment": {
                "aiFoundryName": "aiFoundryName",
                "aiFoundryEndpoint": "aiFoundryEndpoint",
                "aiFoundryProjectEndpoint": "aiFoundryProjectEndpoint",
                "aiFoundryOpenAiEndpoint": "aiFoundryOpenAiEndpoint",
                "aiFoundryPrincipalId": "aiFoundryPrincipalId",
                "aiProjectName": "aiProjectName",
                "modelName": "modelDeploymentName",
            },
            "aksDeployment": {
                "aksClusterName": "aksClusterName",
                "aksClusterId": "aksClusterId",
                "aksClusterFqdn": "aksClusterFqdn",
                "aksKubeletIdentityClientId": "kubeletIdentityClientId",
            },
            "aksFederatedIdentityDeployment": {"workloadIdentityClientId": "clientId"},
            "serviceBusDeployment": {
                "serviceBusNamespaceName": "serviceBusNamespaceName",
                "serviceBusQueueName": "documentProcessingQueueName",
            },
            "eventGridDeployment": {
                "eventGridSystemTopicId": "systemTopicId",
                "eventGridEventSubscriptionId": "eventSubscriptionId",
            },
            "keyVaultDeployment": {
                "keyVaultName": "keyVaultName",
                "keyVaultUri": "keyVaultUri",
                "qdrantApiKeySecretName": "qdrantApiKeySecretName",
                "qdrantReadOnlyApiKeySecretName": "qdrantReadOnlyApiKeySecretName",
                "applicationInsightsConnectionStringSecretName": "applicationInsightsConnectionStringSecretName",
            },
            "monitoringDeployment": {
                "azureMonitorWorkspaceId": "azureMonitorWorkspaceId",
                "logAnalyticsWorkspaceId": "logAnalyticsWorkspaceId",
            },
        }
        expected = {"resourceGroup", "subscriptionId", "tenantId"}
        for deployment, mapping in outputs.items():
            expected.update(mapping)
            for output, source in mapping.items():
                expression = self.template["outputs"][output]["value"]
                self.assertIn(f"'{deployment}'", expression)
                self.assertTrue(expression.endswith(f".outputs.{source}.value]"))
                self.assertEqual(
                    self.wrapper(deployment)["outputs"][source]["type"], "string"
                )
        self.assertEqual(set(self.template["outputs"]), expected)
        self.assertTrue(
            all(item["type"] == "string" for item in self.template["outputs"].values())
        )
        for name, expression in {
            "resourceGroup": "[resourceGroup().name]",
            "subscriptionId": "[subscription().subscriptionId]",
            "tenantId": "[tenant().tenantId]",
        }.items():
            self.assertEqual(self.template["outputs"][name]["value"], expression)

    def test_avm_coverage_and_telemetry(self):
        custom_types = []
        for outer in resources(self.template):
            self.assertEqual(outer["type"], "Microsoft.Resources/deployments")
            for inner in resources(outer["properties"]["template"]):
                if inner.get("existing"):
                    continue
                if inner["type"] == "Microsoft.Resources/deployments":
                    if inner["name"] == "aiFoundryOutputs":
                        self.assertEqual(outer["name"], "aiFoundryDeployment")
                        self.assertFalse(resources(inner["properties"]["template"]))
                        continue
                    self.assertIs(parameters(inner)["enableTelemetry"], False)
                    self.assertIn(
                        "enableTelemetry", inner["properties"]["template"]["parameters"]
                    )
                else:
                    custom_types.append(inner["type"])
        self.assertCountEqual(
            custom_types,
            [
                "Microsoft.Monitor/accounts",
                "Microsoft.Insights/dataCollectionRuleAssociations",
            ],
        )
        for source in (INFRA / "src" / "modules").glob("*.bicep"):
            for reference in re.findall(r"module \w+ '([^']+)'", source.read_text()):
                if (
                    source.name == "aiFoundry.bicep"
                    and reference == "aiFoundryOutputs.bicep"
                ):
                    continue
                self.assertRegex(
                    reference, r"^br/public:avm/(res|ptn)/[\w/-]+:\d+\.\d+\.\d+$"
                )

    def test_arm_template_size(self):
        self.assertLess(len(json.dumps(self.template).encode("utf-8")), 4 * 1024 * 1024)

    def test_deployment_script_environment_contract(self):
        exported = {
            re.sub(r"([a-z])([A-Z])", r"\1_\2", name).upper()
            for name in self.template["outputs"]
        }
        image_tags = {
            "DOCUMENT_PROCESSOR_IMAGE_TAG",
            "COLPALI_INFERENCE_IMAGE_TAG",
            "COLPALI_INFERENCE_VLLM_IMAGE_TAG",
            "AGENT_API_IMAGE_TAG",
            "AGENT_UI_IMAGE_TAG",
        }
        scripts = INFRA.parents[1] / "scripts"
        for name in ("apply_helm.ps1", "build_container.ps1"):
            references = set(
                re.findall(
                    r"\$(?:env|envVars)\['([^']+)'\]",
                    (scripts / name).read_text(encoding="utf-8"),
                )
            )
            self.assertTrue(references)
            self.assertEqual(references - image_tags - exported, set(), name)
        self.assertTrue(
            {
                "AI_FOUNDRY_OPEN_AI_ENDPOINT",
                "WORKLOAD_IDENTITY_CLIENT_ID",
                "AKS_KUBELET_IDENTITY_CLIENT_ID",
                "QDRANT_READ_ONLY_API_KEY_SECRET_NAME",
            }.issubset(exported)
        )

    def test_acr_storage_and_queue_settings(self):
        self.assert_subset(
            self.avm("acrDeployment", "containerRegistry"),
            {
                "acrSku": "[parameters('acrSku')]",
                "acrAdminUserEnabled": False,
                "autoGeneratedDomainNameLabelScope": "Unsecure",
                "roleAssignmentMode": "LegacyRegistryPermissions",
                "publicNetworkAccess": "Enabled",
                "zoneRedundancy": "Disabled",
                "networkRuleSetDefaultAction": "Allow",
                "retentionPolicyStatus": "disabled",
                "azureADAuthenticationAsArmPolicyStatus": "enabled",
                "exportPolicyStatus": "enabled",
            },
        )
        storage = self.avm("dataStorageDeployment", "dataStorageAccount")
        self.assert_subset(
            storage,
            {
                "skuName": "[parameters('storageAccountSku')]",
                "kind": "StorageV2",
                "allowSharedKeyAccess": False,
                "allowBlobPublicAccess": False,
                "requireInfrastructureEncryption": False,
                "minimumTlsVersion": "TLS1_2",
                "supportsHttpsTrafficOnly": True,
                "publicNetworkAccess": "Enabled",
                "networkAcls": {"defaultAction": "Allow", "bypass": "AzureServices"},
            },
        )
        self.assert_subset(
            storage["blobServices"],
            {
                "containerDeleteRetentionPolicyEnabled": False,
                "deleteRetentionPolicyEnabled": False,
                "containers": [
                    {"name": name, "publicAccess": "None"}
                    for name in [
                        "documents",
                        "documents-page-images",
                        "benchmark-datasets",
                        "benchmark-datasets-images",
                    ]
                ],
            },
        )
        bus = self.avm("serviceBusDeployment", "serviceBusNamespace")
        self.assert_subset(
            bus,
            {
                "skuObject": {"name": "[parameters('serviceBusSku')]"},
                "disableLocalAuth": True,
                "authorizationRules": [],
                "zoneRedundant": False,
            },
        )
        self.assertEqual(len(bus["queues"]), 1)
        self.assert_subset(
            bus["queues"][0],
            {
                "lockDuration": "PT5M",
                "maxSizeInMegabytes": 1024,
                "requiresDuplicateDetection": True,
                "duplicateDetectionHistoryTimeWindow": "PT10M",
                "requiresSession": False,
                "defaultMessageTimeToLive": "P14D",
                "deadLetteringOnMessageExpiration": True,
                "maxDeliveryCount": 5,
                "enableBatchedOperations": True,
                "autoDeleteOnIdle": "P10675199DT2H48M5.4775807S",
                "enablePartitioning": False,
            },
        )

    def test_networking_does_not_enable_private_endpoints(self):
        for wrapper, name in (
            ("acrDeployment", "containerRegistry"),
            ("dataStorageDeployment", "dataStorageAccount"),
            ("serviceBusDeployment", "serviceBusNamespace"),
            ("keyVaultDeployment", "keyVault"),
        ):
            with self.subTest(module=name):
                module = resource(self.wrapper(wrapper), name)
                supplied = parameters(module)
                template = module["properties"]["template"]
                self.assertNotIn("privateEndpoints", supplied)
                private_endpoints = template["parameters"]["privateEndpoints"]
                self.assertIs(private_endpoints["nullable"], True)
                self.assertNotIn("defaultValue", private_endpoints)
                endpoint_modules = [
                    entry
                    for entry in resources(template)
                    if entry.get("copy", {})
                    .get("name", "")
                    .endswith("_privateEndpoints")
                ]
                self.assertEqual(len(endpoint_modules), 1)
                self.assertEqual(
                    endpoint_modules[0]["copy"]["count"],
                    "[length(coalesce(parameters('privateEndpoints'), createArray()))]",
                )
                if name != "keyVault":
                    self.assertEqual(supplied["publicNetworkAccess"], "Enabled")

        vault = self.avm("keyVaultDeployment", "keyVault")
        self.assertEqual(
            vault["networkAcls"], {"defaultAction": "Allow", "bypass": "AzureServices"}
        )
        self.assertNotIn("publicNetworkAccess", vault)
        vault_template = resource(self.wrapper("keyVaultDeployment"), "keyVault")[
            "properties"
        ]["template"]
        self.assertEqual(
            vault_template["parameters"]["publicNetworkAccess"]["defaultValue"], ""
        )

        aks_module = resource(self.wrapper("aksDeployment"), "aksCluster")
        self.assertNotIn("apiServerAccessProfile", parameters(aks_module))
        profile = aks_module["properties"]["template"]["parameters"][
            "apiServerAccessProfile"
        ]
        self.assertIs(profile["nullable"], True)
        self.assertNotIn("defaultValue", profile)

        foundry_module = resource(self.wrapper("aiFoundryDeployment"), "aiFoundry")
        foundry = parameters(foundry_module)
        self.assertNotIn("privateEndpointSubnetResourceId", foundry)
        self.assertNotIn("networking", foundry["aiFoundryConfiguration"])
        subnet = foundry_module["properties"]["template"]["parameters"][
            "privateEndpointSubnetResourceId"
        ]
        self.assertIs(subnet["nullable"], True)
        self.assertNotIn("defaultValue", subnet)
        self.assertIs(foundry["includeAssociatedResources"], False)

    def test_aks_pools_and_monitoring(self):
        aks = self.avm("aksDeployment", "aksCluster")
        self.assert_subset(
            aks,
            {
                "skuTier": "Free",
                "publicNetworkAccess": "Enabled",
                "disableLocalAccounts": False,
                "networkPlugin": "azure",
                "networkPolicy": "azure",
                "omsAgentEnabled": False,
                "azurePolicyEnabled": False,
                "enableKeyvaultSecretsProvider": True,
                "enableSecretRotation": True,
                "enableOidcIssuerProfile": True,
                "securityProfile": {"workloadIdentity": {"enabled": True}},
                "autoUpgradeProfile": {"upgradeChannel": "patch"},
                "workloadAutoScalerProfile": {"keda": {"enabled": False}},
                "costAnalysisEnabled": False,
            },
        )
        for driver in (
            "BlobCSIDriver",
            "DiskCSIDriver",
            "FileCSIDriver",
            "SnapshotController",
        ):
            self.assertIs(aks[f"enableStorageProfile{driver}"], True)
        pools = aks["primaryAgentPoolProfiles"] + aks["agentPools"]
        self.assertEqual(
            [pool["name"] for pool in pools], ["systempool", "cpupool", "gpupool"]
        )
        for pool in aks["agentPools"]:
            self.assertEqual(pool["nodeLabels"]["agentpool"], pool["name"])
        for pool, vm, count, minimum, maximum in zip(
            pools,
            ["Standard_D4s_v3", "Standard_D8s_v3", "standard_nv18ads_a10_v5"],
            [2, 0, 0],
            [1, 0, 0],
            [5, 5, 3],
        ):
            self.assert_subset(
                pool,
                {
                    "vmSize": vm,
                    "count": count,
                    "minCount": minimum,
                    "maxCount": maximum,
                    "availabilityZones": [],
                    "osSKU": "Ubuntu",
                    "osDiskType": "Managed",
                    "enableAutoScaling": True,
                },
            )
        self.assert_subset(
            pools[2],
            {
                "scaleSetPriority": "Spot",
                "scaleSetEvictionPolicy": "Delete",
                "spotMaxPrice": -1,
                "nodeTaints": ["sku=gpu:NoSchedule"],
            },
        )
        logs = aks["diagnosticSettings"][0]["logCategoriesAndGroups"]
        self.assertEqual(len(logs), 11)
        self.assertEqual(
            {item["category"] for item in logs if not item["enabled"]},
            {"kube-audit", "kube-audit-admin"},
        )
        self.assert_subset(
            self.avm("monitoringDeployment", "logAnalyticsWorkspace"),
            {
                "dataRetention": "[parameters('retentionInDays')]",
                "dailyQuotaGb": "2",
                "forceCmkForQuery": False,
                "features": {
                    "enableLogAccessUsingOnlyResourcePermissions": True,
                    "disableLocalAuth": False,
                },
            },
        )
        self.assert_subset(
            self.avm("monitoringDeployment", "applicationInsights"),
            {
                "ingestionMode": "LogAnalytics",
                "disableIpMasking": False,
                "retentionInDays": 90,
            },
        )

    def test_helm_pool_names_match_infrastructure(self):
        aks = self.avm("aksDeployment", "aksCluster")
        pool_names = {pool["name"] for pool in aks["agentPools"]}
        gpu_pool = next(
            pool["name"]
            for pool in aks["agentPools"]
            if pool["nodeLabels"]["workload"] == "gpu"
        )
        chart = INFRA.parent / "helm" / "colpali-stack"
        values = (chart / "values.yaml").read_text(encoding="utf-8")
        self.assertIn(f'nodePool: "{gpu_pool}"', values)
        inference = (chart / "templates" / "colpali-inference.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            f'.Values.colpaliInference.nodePool | default "{gpu_pool}"', inference
        )
        sources = [values] + [
            path.read_text(encoding="utf-8")
            for path in (chart / "templates").glob("*.yaml")
        ]
        selectors = re.findall(
            r"^\s*kubernetes\.azure\.com/agentpool:\s*([a-z0-9]+)\s*$",
            "\n".join(sources),
            re.MULTILINE,
        )
        self.assertTrue(selectors)
        self.assertTrue(set(selectors).issubset(pool_names))

    def test_aks_version_is_not_pinned_by_default(self):
        wrapper = self.wrapper("aksDeployment")
        version = wrapper["parameters"]["kubernetesVersion"]
        self.assertEqual(version["type"], "string")
        self.assertIs(version["nullable"], True)
        self.assertNotIn("defaultValue", version)
        self.assertNotIn(
            "kubernetesVersion",
            parameters(resource(self.template, "aksDeployment")),
        )
        aks = self.avm("aksDeployment", "aksCluster")
        self.assertEqual(aks["kubernetesVersion"], "[parameters('kubernetesVersion')]")
        self.assertEqual(aks["autoUpgradeProfile"], {"upgradeChannel": "patch"})
        for pool in aks["primaryAgentPoolProfiles"] + aks["agentPools"]:
            self.assertNotIn("orchestratorVersion", pool)

        avm_template = resource(wrapper, "aksCluster")["properties"]["template"]
        self.assertIs(avm_template["parameters"]["kubernetesVersion"]["nullable"], True)
        pool_module = avm_template["resources"]["managedCluster_agentPools"]
        self.assertEqual(
            parameters(pool_module)["orchestratorVersion"],
            "[coalesce(tryGet(coalesce(parameters('agentPools'), createArray())[copyIndex()], 'orchestratorVersion'), parameters('kubernetesVersion'))]",
        )
        self.assertIs(
            pool_module["properties"]["template"]["parameters"]["orchestratorVersion"][
                "nullable"
            ],
            True,
        )

    def test_gpu_monitor_uses_managed_prometheus(self):
        aks = self.avm("aksDeployment", "aksCluster")
        self.assertIs(aks["azureMonitorProfile"]["metrics"]["enabled"], True)
        manifest = (INFRA / "src" / "k8s" / "dcgm-exporter.yaml").read_text(
            encoding="utf-8"
        )
        self.assertRegex(
            manifest,
            r"apiVersion: azmonitoring\.coreos\.com/v1\s+kind: ServiceMonitor",
        )

    def test_foundry_secrets_and_federation(self):
        foundry = self.avm("aiFoundryDeployment", "aiFoundry")
        self.assert_subset(
            foundry,
            {
                "location": "[parameters('aiFoundryLocation')]",
                "includeAssociatedResources": False,
                "aiFoundryConfiguration": {
                    "accountName": "[parameters('aiFoundryName')]",
                    "location": "[parameters('aiFoundryLocation')]",
                    "sku": "S0",
                    "allowProjectManagement": True,
                    "disableLocalAuth": True,
                    "createCapabilityHosts": False,
                    "project": {
                        "name": "[parameters('aiProjectName')]",
                        "displayName": "[parameters('aiProjectName')]",
                        "desc": "[parameters('aiProjectName')]",
                    },
                },
                "aiModelDeployments": [
                    {
                        "name": "gpt-4.1",
                        "sku": {"capacity": 100, "name": "GlobalStandard"},
                        "model": {
                            "name": "gpt-4.1",
                            "version": "2025-04-14",
                            "format": "OpenAI",
                        },
                    }
                ],
            },
        )
        vault = self.avm("keyVaultDeployment", "keyVault")
        self.assert_subset(
            vault,
            {
                "sku": "standard",
                "enableRbacAuthorization": True,
                "enablePurgeProtection": False,
                "enableVaultForDeployment": False,
                "enableVaultForTemplateDeployment": False,
                "enableVaultForDiskEncryption": False,
            },
        )
        self.assertEqual(
            [secret["name"] for secret in vault["secrets"]],
            [
                "qdrant-api-key",
                "qdrant-readonly-api-key",
                "appinsights-connection-string",
            ],
        )
        for name in ("qdrantApiKey", "qdrantReadOnlyApiKey"):
            param = self.wrapper("keyVaultDeployment")["parameters"][name]
            self.assertEqual(param["type"], "securestring")
            self.assertIn("newGuid()", param["defaultValue"])
        identity = self.avm("aksFederatedIdentityDeployment", "workloadIdentity")
        self.assertEqual(
            identity["federatedIdentityCredentials"],
            [
                {
                    "name": "workload-federated-identity",
                    "issuer": "[parameters('oidcIssuerUrl')]",
                    "subject": "[format('system:serviceaccount:{0}:{1}', parameters('namespace'), parameters('serviceAccountName'))]",
                    "audiences": ["api://AzureADTokenExchange"],
                }
            ],
        )

    def test_foundry_pattern_resources_and_outputs(self):
        wrapper = self.wrapper("aiFoundryDeployment")
        pattern = resource(wrapper, "aiFoundry")["properties"]["template"]
        for name in ("keyVault", "aiSearch", "storageAccount", "cosmosDb"):
            self.assertEqual(
                pattern["resources"][name]["condition"],
                "[parameters('includeAssociatedResources')]",
            )
        account_wrapper = pattern["resources"]["foundryAccount"]["properties"][
            "template"
        ]
        account_module = next(
            entry
            for entry in resources(account_wrapper)
            if entry["type"] == "Microsoft.Resources/deployments"
        )
        account_params = account_module["properties"]["parameters"]
        self.assertEqual(account_params["kind"]["value"], "AIServices")
        self.assertEqual(
            account_params["managedIdentities"]["value"], {"systemAssigned": True}
        )
        self.assertEqual(
            account_params["customSubDomainName"]["value"], "[parameters('name')]"
        )
        self.assertEqual(
            account_params["deployments"]["value"], "[parameters('aiModelDeployments')]"
        )
        self.assertEqual(
            account_params["publicNetworkAccess"],
            "[if(variables('privateNetworkingEnabled'), createObject('value', 'Disabled'), createObject('value', 'Enabled'))]",
        )
        self.assertNotIn(
            "privateEndpointSubnetResourceId",
            self.avm("aiFoundryDeployment", "aiFoundry"),
        )
        # Keep the documented pattern limitation visible when upgrading AVM.
        self.assertNotIn("restrictOutboundNetworkAccess", account_params)
        self.assertIs(
            account_module["properties"]["template"]["parameters"][
                "restrictOutboundNetworkAccess"
            ]["defaultValue"],
            True,
        )
        project_template = pattern["resources"]["foundryProject"]["properties"][
            "template"
        ]
        project = next(
            entry
            for entry in resources(project_template)
            if entry["type"] == "Microsoft.CognitiveServices/accounts/projects"
        )
        self.assertEqual(project["identity"], {"type": "SystemAssigned"})
        self.assertEqual(
            project["name"],
            "[format('{0}/{1}', parameters('accountName'), parameters('name'))]",
        )
        outputs = wrapper["outputs"]
        self.assertTrue(
            outputs["aiFoundryName"]["value"].endswith(".outputs.aiServicesName.value]")
        )
        self.assertTrue(
            outputs["aiProjectName"]["value"].endswith(".outputs.aiProjectName.value]")
        )
        self.assertEqual(outputs["modelDeploymentName"]["value"], "gpt-4.1")
        lookup = resource(wrapper, "aiFoundryOutputs")
        self.assertEqual(
            lookup["dependsOn"],
            ["[resourceId('Microsoft.Resources/deployments', 'aiFoundry')]"],
        )
        self.assertTrue(
            parameters(lookup)["aiFoundryName"].endswith(
                ".outputs.aiServicesName.value]"
            )
        )
        self.assertTrue(
            parameters(lookup)["aiProjectName"].endswith(
                ".outputs.aiProjectName.value]"
            )
        )
        lookup_template = lookup["properties"]["template"]
        self.assertFalse(resources(lookup_template))
        lookup_outputs = lookup_template["outputs"]
        for name in (
            "aiFoundryId",
            "aiProjectId",
            "aiFoundryEndpoint",
            "aiFoundryPrincipalId",
            "aiProjectPrincipalId",
            "aiFoundryOpenAiEndpoint",
            "aiFoundryProjectEndpoint",
        ):
            self.assertIn("'aiFoundryOutputs'", outputs[name]["value"])
            self.assertTrue(outputs[name]["value"].endswith(f".outputs.{name}.value]"))
            self.assertEqual(lookup_outputs[name]["type"], "string")
        self.assertEqual(
            lookup_outputs["aiFoundryId"]["value"],
            "[resourceId('Microsoft.CognitiveServices/accounts', parameters('aiFoundryName'))]",
        )
        self.assertEqual(
            lookup_outputs["aiProjectId"]["value"],
            "[resourceId('Microsoft.CognitiveServices/accounts/projects', parameters('aiFoundryName'), parameters('aiProjectName'))]",
        )
        for name in (
            "aiFoundryEndpoint",
            "aiFoundryPrincipalId",
            "aiProjectPrincipalId",
        ):
            self.assertIn("reference(resourceId(", lookup_outputs[name]["value"])
        self.assertIn(
            ".customSubDomainName", lookup_outputs["aiFoundryOpenAiEndpoint"]["value"]
        )
        self.assertIn(
            ".customSubDomainName", lookup_outputs["aiFoundryProjectEndpoint"]["value"]
        )

    def test_event_delivery_and_rbac_dependencies(self):
        event = self.wrapper("eventGridDeployment")
        topic = resource(event, "blobSystemTopic")
        self.assertEqual(len(topic["dependsOn"]), 3)
        for role in (
            "eventGridServiceBusDataSenderRole",
            "eventGridServiceBusDataReceiverRole",
        ):
            self.assertTrue(any(role in entry for entry in topic["dependsOn"]))
        subscription = parameters(topic)["eventSubscriptions"][0]
        self.assertEqual(
            subscription["retryPolicy"],
            {
                "maxDeliveryAttempts": 5,
                "eventTimeToLiveInMinutes": 180,
            },
        )
        self.assertEqual(
            subscription["filter"],
            {
                "includedEventTypes": [
                    "Microsoft.Storage.BlobCreated",
                    "Microsoft.Storage.BlobDeleted",
                ],
                "subjectBeginsWith": "/blobServices/default/containers/documents/blobs/",
                "subjectEndsWith": ".pdf",
            },
        )
        self.assertEqual(
            subscription["deliveryWithResourceIdentity"]["destination"]["endpointType"],
            "ServiceBusQueue",
        )
        roles = resources(self.wrapper("roleAssignmentsDeployment"))
        self.assertEqual(len(roles), 11)
        names = {
            "aksDataStorageAssignment": (
                "storageBlobDataContributor",
                "aks-data-storage",
            ),
            "aksServiceBusSenderAssignment": (
                "serviceBusDataSender",
                "aks-servicebus-sender",
            ),
            "aksServiceBusReceiverAssignment": (
                "serviceBusDataReceiver",
                "aks-servicebus-receiver",
            ),
            "aksClusterAcrPullAssignment": ("acrPull", "aks-cluster-acr-pull"),
            "aksKeyVaultSecretsUserAssignment": (
                "keyVaultSecretsUser",
                "aks-keyvault-secrets-user",
            ),
            "aksAIFoundryProjectAssignment": ("azureAIUser", "aks-ai-foundry-project"),
            "aksAIFoundryOpenAIAssignment": (
                "cognitiveServicesOpenAIUser",
                "aks-ai-foundry-openai",
            ),
            "userDataStorageAssignment": (
                "storageBlobDataContributor",
                "user-data-storage",
            ),
            "userAIFoundryAssignment": ("azureAIUser", "user-ai-foundry"),
            "userAcrPushAssignment": ("acrPush", "user-acr-push"),
            "userAcrPullAssignment": ("acrPull", "user-acr-pull"),
        }
        for role in roles:
            self.assertIn("parameters('deployRoleAssignments')", role["condition"])
            params = parameters(role)
            definition, suffix = names[role["name"]]
            self.assertEqual(
                params["name"],
                f"[guid({params['resourceId'][1:-1]}, {params['principalId'][1:-1]}, "
                f"variables('roles').{definition}, '{suffix}')]",
            )
            self.assertEqual(
                params["roleDefinitionId"],
                f"[subscriptionResourceId('Microsoft.Authorization/roleDefinitions', variables('roles').{definition})]",
            )
            self.assertEqual(
                params["principalType"],
                "User" if role["name"].startswith("user") else "ServicePrincipal",
            )


if __name__ == "__main__":
    unittest.main()
