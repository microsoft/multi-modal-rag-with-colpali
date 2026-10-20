# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Run the deployment script offline with mocked Azure CLI and kubectl commands."""

import base64
import json
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

HARNESS = r"""
$ErrorActionPreference = "Stop"
$fixture = Get-Content (Join-Path $PSScriptRoot "fixture.json") -Raw | ConvertFrom-Json
$calls = [Collections.Generic.List[object]]::new()
function az {
    $calls.Add(@("az") + @($args))
    $global:LASTEXITCODE = 0
    switch ("$($args[0]) $($args[1])") {
        "account show" {
            if ($fixture.accountFailure) { $global:LASTEXITCODE = 1; return }
            $fixture.account | ConvertTo-Json -Depth 10
        }
        "account get-access-token" {
            if ($fixture.tokenFailure) { $global:LASTEXITCODE = 1; return }
            $fixture.token | ConvertTo-Json -Depth 10
        }
        "ad signed-in-user" { $fixture.homeUserId }
        "deployment group" {
            '{"aksClusterName":{"value":"aks-test"},"resourceGroup":{"value":"test-rg"}}'
        }
        "aks get-credentials" {}
        default { throw "Unexpected Azure command: $args" }
    }
}
function kubectl {
    $calls.Add(@("kubectl") + @($args))
    $global:LASTEXITCODE = [int]$fixture.bootstrapFailure
}
$failed = $false
try {
    $parameters = @{ ResourceGroup = "test-rg" }
    if ($fixture.explicitSubscription) { $parameters.Subscription = "target-subscription" }
    & (Join-Path $PSScriptRoot "scripts/deploy_infra.ps1") @parameters
} catch {
    Write-Output $_.Exception.Message
    $failed = $true
} finally {
    ConvertTo-Json -InputObject @($calls.ToArray()) -Depth 10 |
        Set-Content (Join-Path $PSScriptRoot "calls.json")
}
if ($failed) { exit 1 }
"""


class DeploymentScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pwsh = shutil.which("pwsh")
        if not cls.pwsh:
            raise RuntimeError(
                "PowerShell (pwsh) is required to test deployment scripts."
            )

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name).resolve()
        (self.directory / "scripts").mkdir()
        shutil.copyfile(
            ROOT / "scripts" / "deploy_infra.ps1",
            self.directory / "scripts" / "deploy_infra.ps1",
        )
        (self.directory / ".env").write_text("KEEP_ME=existing", encoding="utf-8")
        self.account = {
            "id": str(uuid.uuid4()),
            "tenantId": str(uuid.uuid4()),
            "user": {"type": "user"},
        }
        self.claims = {"tid": self.account["tenantId"], "oid": str(uuid.uuid4())}
        self.fixture = {
            "account": self.account,
            "homeUserId": str(uuid.uuid4()),
            "explicitSubscription": True,
        }

    def run_script(self):
        payload = base64.urlsafe_b64encode(json.dumps(self.claims).encode())
        self.fixture["token"] = {
            "accessToken": f"header.{payload.decode().rstrip('=')}.signature",
            "tenant": self.account["tenantId"],
            "subscription": self.account["id"],
        }
        (self.directory / "fixture.json").write_text(
            json.dumps(self.fixture), encoding="utf-8"
        )
        harness = self.directory / "run.ps1"
        harness.write_text(HARNESS, encoding="utf-8")
        self.result = subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-File", str(harness)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.calls = json.loads(
            (self.directory / "calls.json").read_text(encoding="utf-8-sig")
        )
        self.assertNotIn(
            self.fixture["token"]["accessToken"],
            self.result.stdout + self.result.stderr,
        )
        return self.result

    def assert_stopped_before_deployment(self, message):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(message, result.stdout)
        self.assertFalse(any(call[1] == "deployment" for call in self.calls))
        self.assertEqual(
            (self.directory / ".env").read_text(encoding="utf-8"), "KEEP_ME=existing"
        )

    def test_subscription_scoped_guest_identity_and_bootstrap(self):
        for explicit in (True, False):
            with self.subTest(explicit_subscription=explicit):
                self.fixture["explicitSubscription"] = explicit
                result = self.run_script()
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                account_call = self.calls[0]
                self.assertEqual("--subscription" in account_call, explicit)
                if explicit:
                    self.assertIn("target-subscription", account_call)
                azure_calls = [call for call in self.calls if call[0] == "az"]
                self.assertEqual(len(azure_calls), 4)
                for call in azure_calls[1:]:
                    self.assertEqual(
                        call[call.index("--subscription") + 1], self.account["id"]
                    )
                deployment = next(
                    call for call in azure_calls if call[1] == "deployment"
                )
                self.assertIn(f"userObjectId={self.claims['oid']}", deployment)
                self.assertNotIn(
                    f"userObjectId={self.fixture['homeUserId']}", deployment
                )
                self.assertEqual(
                    len([call for call in self.calls if call[0] == "kubectl"]), 4
                )
                env = (self.directory / ".env").read_text(encoding="utf-8")
                self.assertIn("KEEP_ME=existing", env)
                self.assertIn("AKS_CLUSTER_NAME=aks-test", env)
                self.assertNotIn(self.fixture["token"]["accessToken"], env)

    def test_wrong_tenant_stops_before_deployment(self):
        self.claims["tid"] = str(uuid.uuid4())
        self.assert_stopped_before_deployment("does not match")

    def test_missing_user_id_stops_before_deployment(self):
        del self.claims["oid"]
        self.assert_stopped_before_deployment("valid user object ID")

    def test_non_user_login_stops_before_deployment(self):
        self.account["user"]["type"] = "servicePrincipal"
        self.assert_stopped_before_deployment("interactive Azure user login")

    def test_account_failure_stops_before_deployment(self):
        self.fixture["accountFailure"] = True
        self.assert_stopped_before_deployment("Failed to resolve Azure subscription")

    def test_token_failure_stops_before_deployment(self):
        self.fixture["tokenFailure"] = True
        self.assert_stopped_before_deployment("Failed to obtain an ARM token")

    def test_bootstrap_failure_is_not_reported_as_success(self):
        self.fixture["bootstrapFailure"] = True
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Failed to apply Kubernetes manifest", result.stdout)
        self.assertNotIn("Infrastructure deployment complete!", result.stdout)
        self.assertEqual(len([call for call in self.calls if call[0] == "kubectl"]), 1)


HELM_HARNESS = r"""
$ErrorActionPreference = "Stop"
$fixture = Get-Content (Join-Path $PSScriptRoot "fixture.json") -Raw | ConvertFrom-Json
$calls = [Collections.Generic.List[object]]::new()
function az {
    $calls.Add(@("az") + @($args))
    $global:LASTEXITCODE = 0
}
function helm {
    $calls.Add(@("helm") + @($args))
    $global:LASTEXITCODE = 0
    if ($args[0] -eq "dependency") { $global:LASTEXITCODE = [int]$fixture.dependencyFailure }
    elseif ($args[0] -eq "upgrade") { $global:LASTEXITCODE = [int]$fixture.helmFailure }
    else { throw "Unexpected Helm command: $args" }
}
function kubectl {
    $calls.Add(@("kubectl") + @($args))
    $global:LASTEXITCODE = 0
    if ($args -contains "svc") { "203.0.113.10" }
}
$failed = $false
try {
    $parameters = @{ LocalAccess = $true }
    if ($fixture.isolated) {
        $parameters.EnvFile = Join-Path $PSScriptRoot "test.env"
        $parameters.Subscription = "test-subscription"
        $parameters.KubeConfig = Join-Path $PSScriptRoot "test.kubeconfig"
    }
    & (Join-Path $PSScriptRoot "scripts/apply_helm.ps1") @parameters
} catch {
    Write-Output $_.Exception.Message
    $failed = $true
} finally {
    ConvertTo-Json -InputObject @($calls.ToArray()) -Depth 10 |
        Set-Content (Join-Path $PSScriptRoot "calls.json")
}
if ($failed) { exit 1 }
"""


class HelmDeploymentScriptTests(unittest.TestCase):
    def setUp(self):
        self.pwsh = shutil.which("pwsh")
        if not self.pwsh:
            raise RuntimeError(
                "PowerShell (pwsh) is required to test deployment scripts."
            )
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name).resolve()
        (self.directory / "scripts").mkdir()
        (self.directory / "modules" / "helm" / "colpali-stack").mkdir(parents=True)
        shutil.copyfile(
            ROOT / "scripts" / "apply_helm.ps1",
            self.directory / "scripts" / "apply_helm.ps1",
        )
        names = (
            "RESOURCE_GROUP AKS_CLUSTER_NAME ACR_LOGIN_SERVER "
            "WORKLOAD_IDENTITY_CLIENT_ID AKS_KUBELET_IDENTITY_CLIENT_ID TENANT_ID "
            "DATA_STORAGE_ACCOUNT_NAME SERVICE_BUS_NAMESPACE_NAME SERVICE_BUS_QUEUE_NAME "
            "KEY_VAULT_NAME QDRANT_API_KEY_SECRET_NAME QDRANT_READ_ONLY_API_KEY_SECRET_NAME "
            "APPLICATION_INSIGHTS_CONNECTION_STRING_SECRET_NAME "
            "AI_FOUNDRY_OPEN_AI_ENDPOINT MODEL_NAME"
        ).split()
        self.config = "\n".join(f"{name}=test-{name.lower()}" for name in names)
        (self.directory / "test.env").write_text(self.config, encoding="utf-8")
        (self.directory / ".env").write_text("ORIGINAL=unchanged", encoding="utf-8")
        self.fixture = {"isolated": True}

    def run_script(self):
        (self.directory / "fixture.json").write_text(
            json.dumps(self.fixture), encoding="utf-8"
        )
        harness = self.directory / "run.ps1"
        harness.write_text(HELM_HARNESS, encoding="utf-8")
        result = subprocess.run(
            [self.pwsh, "-NoProfile", "-NonInteractive", "-File", str(harness)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.calls = json.loads(
            (self.directory / "calls.json").read_text(encoding="utf-8-sig")
        )
        return result

    def test_isolated_configuration_and_kubeconfig(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        azure = next(call for call in self.calls if call[0] == "az")
        self.assertEqual(azure[azure.index("--subscription") + 1], "test-subscription")
        self.assertEqual(
            Path(azure[azure.index("--file") + 1]),
            self.directory / "test.kubeconfig",
        )
        for call in self.calls:
            if call[0] == "kubectl" or call[:2] == ["helm", "upgrade"]:
                self.assertEqual(
                    Path(call[call.index("--kubeconfig") + 1]),
                    self.directory / "test.kubeconfig",
                )
        self.assertIn(["helm", "dependency", "build"], self.calls)
        self.assertEqual(
            (self.directory / ".env").read_text(encoding="utf-8"), "ORIGINAL=unchanged"
        )
        self.assertIn(
            "AGENT_UI_ENDPOINT=http://203.0.113.10",
            (self.directory / "test.env").read_text(encoding="utf-8"),
        )

    def test_default_configuration_is_preserved(self):
        self.fixture["isolated"] = False
        (self.directory / ".env").write_text(self.config, encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for call in self.calls:
            self.assertNotIn("--subscription", call)
            self.assertNotIn("--file", call)
            self.assertNotIn("--kubeconfig", call)
        self.assertEqual(
            (self.directory / "test.env").read_text(encoding="utf-8"), self.config
        )

    def test_dependency_failure_stops_installation(self):
        self.fixture["dependencyFailure"] = True
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Failed to restore Helm dependencies", result.stdout)
        self.assertFalse(any(call[:2] == ["helm", "upgrade"] for call in self.calls))

    def test_helm_failure_stops_success_reporting(self):
        self.fixture["helmFailure"] = True
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Helm deployment failed", result.stdout)
        self.assertNotIn("Deployment complete.", result.stdout)
        self.assertFalse(any(call[0] == "kubectl" for call in self.calls))
