#!/usr/bin/env python3
"""
ColQwen2 Model Deployment Script

Deploys the registered ColQwen2 model to an existing Azure ML Online Endpoint.
The endpoint is created via Bicep infrastructure and this script only handles model deployment.
Reads configuration from root .env file.
"""

import logging
import os
import sys
from pathlib import Path

import tomli
import yaml
from azure.ai.ml import MLClient
from azure.ai.ml.entities import (
    CodeConfiguration,
    Environment,
    ManagedOnlineDeployment,
    OnlineRequestSettings,
)
from azure.identity import DefaultAzureCredential
from dotenv import find_dotenv, load_dotenv


class ColQwen2EndpointDeployer:
    """Deploys and manages ColQwen2 model endpoint."""

    def __init__(self):
        """Initialize the deployer with configuration from .env file."""
        load_dotenv(find_dotenv())

        # Setup logging
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        logging.basicConfig(
            level=getattr(logging, log_level),
            format="%(asctime)s - %(levelname)s - %(message)s",
        )
        self.logger = logging.getLogger(__name__)

        # Get configuration from environment
        self.subscription_id = os.getenv("SUBSCRIPTION_ID")
        self.resource_group = os.getenv("RESOURCE_GROUP")
        self.workspace_name = os.getenv("AML_WORKSPACE_NAME")
        self.endpoint_name = os.getenv(
            "AML_EMBEDDING_ENDPOINT_NAME", "embedding-endpoint"
        )
        self.model_name = "colqwen2-model"
        self.model_version = os.getenv("MODEL_VERSION", "4")
        self.instance_type = os.getenv("AML_EMBEDDING_ENDPOINT_TYPE", "Standard_DS3_v2")
        self.instance_count = int(os.getenv("AML_EMBEDDING_ENDPOINT_COUNT", "1"))

        # Validate required configuration
        if not all([self.subscription_id, self.resource_group, self.workspace_name]):
            self.logger.error("Missing required environment variables")
            self.logger.error(
                "Ensure SUBSCRIPTION_ID, RESOURCE_GROUP, and AML_WORKSPACE_NAME are set in .env file"
            )
            sys.exit(1)

        self.logger.info("Subscription ID: %s", self.subscription_id)
        self.logger.info("Resource Group: %s", self.resource_group)
        self.logger.info("Workspace Name: %s", self.workspace_name)
        self.logger.info("Endpoint Name: %s", self.endpoint_name)

        # Initialize Azure ML client
        try:
            credential = DefaultAzureCredential()
            self.ml_client = MLClient(
                credential=credential,
                subscription_id=self.subscription_id,
                resource_group_name=self.resource_group,
                workspace_name=self.workspace_name,
            )
            self.logger.info("Successfully connected to Azure ML workspace")
        except Exception as e:
            self.logger.error("Failed to connect to Azure ML: %s", str(e))
            sys.exit(1)

        # Set paths
        self.script_dir = Path(__file__).parent.parent
        self.pyproject_path = self.script_dir / "pyproject.toml"
        self.env_yml_path = self.script_dir / "environment.yml"
        self.src_dir = self.script_dir / "src"

    def _parse_pyproject(self):
        """Parse pyproject.toml to extract dependencies for environment creation."""
        if not self.pyproject_path.exists():
            raise FileNotFoundError(
                f"pyproject.toml not found at {self.pyproject_path}"
            )

        with open(self.pyproject_path, "rb") as f:
            pyproject = tomli.load(f)

        return pyproject

    def _generate_environment_yml(self, pyproject, output_path):
        """Generate environment.yml from pyproject.toml main dependencies and score group."""
        # Get all dependencies from main dependencies list
        all_deps = pyproject.get("project", {}).get("dependencies", [])

        # Add score group dependencies (includes flash-attn for inference)
        score_deps = pyproject.get("dependency-groups", {}).get("score", [])
        all_deps.extend(score_deps)

        # Exclude orchestration-only packages (not needed in compute environment)
        exclude_packages = [
            "azure-ai-ml",
            "azure-identity",
            "python-dotenv",
            "tomli",
            "pyyaml",
        ]

        compute_deps = []
        for dep in all_deps:
            # Extract package name (before ==, >=, <=, etc.)
            pkg_name = (
                dep.split("==")[0]
                .split(">=")[0]
                .split("<=")[0]
                .split("~=")[0]
                .split("[")[0]
                .strip()
            )

            # Only exclude if exact package name matches
            if pkg_name not in exclude_packages:
                compute_deps.append(dep)

        if not compute_deps:
            self.logger.warning("No compute dependencies found in pyproject.toml")
            self.logger.warning("All dependencies: %s", all_deps)
            self.logger.warning("Exclude packages: %s", exclude_packages)
            return

        # All dependencies go to pip (torch and torchvision via pip for compatibility)
        pip_deps = []

        for dep in compute_deps:
            pkg_name = (
                dep.split("==")[0]
                .split(">=")[0]
                .split("<=")[0]
                .split("~=")[0]
                .split("[")[0]
                .strip()
            )
            if pkg_name == "flash-attn":
                # Use pre-built wheel from GitHub releases (torch2.7 for exact compatibility)
                wheel_url = "https://github.com/Dao-AILab/flash-attention/releases/download/v2.8.3/flash_attn-2.8.3+cu12torch2.7cxx11abiTRUE-cp311-cp311-linux_x86_64.whl"
                pip_deps.append(wheel_url)
            else:
                pip_deps.append(dep)

        # Create conda environment with minimal conda deps, most packages via pip
        conda_deps = [
            f"python={pyproject.get('project', {}).get('requires-python', '>=3.11').replace('>=', '')}",
            "pytorch-cuda=12.1",
            "pip",
        ]

        env_data = {
            "name": "colpali-endpoint",
            "channels": ["conda-forge", "pytorch", "nvidia"],
            "dependencies": conda_deps + [{"pip": pip_deps}],
        }

        # Write environment.yml
        with open(output_path, "w") as f:
            f.write("# Auto-generated from pyproject.toml by deploy_endpoint.py\n")
            f.write("# This file is regenerated on every deployment\n\n")
            yaml.dump(env_data, f, default_flow_style=False, sort_keys=False)

        self.logger.info(
            "Generated environment.yml with %d dependencies", len(compute_deps)
        )

    def _ensure_environment_yml(self):
        """Ensure environment.yml exists, generate from pyproject.toml if needed."""
        # Always regenerate to ensure it's up to date
        self.logger.info("Generating environment.yml from pyproject.toml")
        pyproject = self._parse_pyproject()
        self._generate_environment_yml(pyproject, self.env_yml_path)
        return self.env_yml_path

    def create_environment(self):
        """Create ColQwen2 environment with dynamically generated environment.yml."""
        # Always generate fresh environment.yml from pyproject.toml
        env_file = self._ensure_environment_yml()

        self.logger.info("Creating environment from generated %s", env_file)

        # Create environment name based on endpoint type
        env_name = "colpali-endpoint-env"

        environment = Environment(
            name=env_name,
            description="ColQwen2 environment with PyTorch, transformers, and pre-built flash-attn",
            conda_file=str(env_file),
            image="mcr.microsoft.com/azureml/openmpi4.1.0-cuda11.8-cudnn8-ubuntu22.04:latest",
        )

        # Register environment (Azure ML will auto-increment version if content changed)
        created_env = self.ml_client.environments.create_or_update(environment)
        self.logger.info(
            "Environment registered: %s (version %s)",
            created_env.name,
            created_env.version,
        )

        return created_env

    def verify_endpoint_exists(self):
        """Verify that the endpoint exists."""
        try:
            endpoint = self.ml_client.online_endpoints.get(self.endpoint_name)
            self.logger.info("Found existing endpoint: %s", endpoint.name)
            return True
        except Exception:
            self.logger.error(
                "Endpoint '%s' not found. Please deploy infrastructure first via Bicep.",
                self.endpoint_name,
            )
            return False

    def deploy_model(self, environment: Environment):
        """Deploy ColQwen2 model to the existing endpoint created by Bicep."""
        self.logger.info("Deploying ColQwen2 model to endpoint: %s", self.endpoint_name)

        # Verify endpoint exists
        if not self.verify_endpoint_exists():
            return None

        # Get registered model
        model = self.ml_client.models.get(self.model_name, version=self.model_version)
        self.logger.info(
            "Using model: %s (version %s)", self.model_name, self.model_version
        )
        self.logger.info(
            "Using instance type: %s (count: %d)",
            self.instance_type,
            self.instance_count,
        )

        deployment = ManagedOnlineDeployment(
            name="embedding-deployment",
            endpoint_name=self.endpoint_name,
            model=model,
            environment=environment,
            code_configuration=CodeConfiguration(
                code=str(self.src_dir), scoring_script="score.py"
            ),
            instance_type=self.instance_type,
            instance_count=self.instance_count,
            request_settings=OnlineRequestSettings(
                max_concurrent_requests_per_instance=8, request_timeout_ms=60000
            ),
            environment_variables={"WORKER_COUNT": "8"},
        )

        created_deployment = self.ml_client.online_deployments.begin_create_or_update(
            deployment
        ).result()
        self.logger.info("Model deployed: %s", created_deployment.name)

        # Set traffic to 100%
        endpoint = self.ml_client.online_endpoints.get(self.endpoint_name)
        endpoint.traffic = {"embedding-deployment": 100}
        self.ml_client.online_endpoints.begin_create_or_update(endpoint).result()

        self.logger.info("Traffic routing configured")
        return created_deployment

    def deploy(self):
        """Execute the full deployment workflow."""
        try:
            self.logger.info(
                "Starting ColQwen2 model deployment to endpoint: %s", self.endpoint_name
            )

            # Create environment
            environment = self.create_environment()

            # Deploy model to existing endpoint (created by Bicep)
            deployment = self.deploy_model(environment)

            if not deployment:
                return False

            # Get final endpoint details
            final_endpoint = self.ml_client.online_endpoints.get(self.endpoint_name)

            self.logger.info("=" * 50)
            self.logger.info("Deployment completed successfully!")
            self.logger.info("Endpoint: %s", self.endpoint_name)
            self.logger.info("Scoring URI: %s", final_endpoint.scoring_uri)
            self.logger.info("Authentication: Managed Identity (AAD Token)")
            self.logger.info("=" * 50)

            return True

        except Exception as e:
            self.logger.error("Failed to deploy endpoint: %s", str(e))
            return False


def main():
    """Main entry point for the deployment script."""
    deployer = ColQwen2EndpointDeployer()
    success = deployer.deploy()

    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
