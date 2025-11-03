"""
ColQwen2 Model Registration Pipeline for Azure ML

This pipeline downloads and registers the ColQwen2 model to Azure ML.
It runs the download on Azure ML compute to speed up the process.
"""

import argparse
import logging
import os
import tomllib
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from azure.ai.ml import MLClient, Output, command, dsl
from azure.ai.ml.constants import AssetTypes
from azure.ai.ml.entities import Environment, Model
from azure.identity import DefaultAzureCredential
from dotenv import find_dotenv, load_dotenv


class ColpaliRegistrationPipeline:
    """
    Base class for Azure ML model setup pipelines.

    Provides common functionality for:
    - Azure ML workspace connection
    - Environment management
    - Compute management
    - Model registration
    - Pipeline orchestration
    """

    def __init__(
        self,
        subscription_id: Optional[str] = None,
        resource_group: Optional[str] = None,
        workspace_name: Optional[str] = None,
        compute_name: Optional[str] = None,
    ):
        """
        Initialize the Azure ML pipeline.

        Args:
            subscription_id: Azure subscription ID (reads from env if not provided)
            resource_group: Azure resource group name (reads from env if not provided)
            workspace_name: Azure ML workspace name (reads from env if not provided)
            compute_name: Azure ML compute name (reads from env if not provided)
        """
        self._setup_logging()
        self._load_config(subscription_id, resource_group, workspace_name, compute_name)
        self._initialize_client()

    def _setup_logging(self):
        """Configure logging for the pipeline."""
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
        self.logger = logging.getLogger(self.__class__.__name__)

        # Suppress verbose Azure SDK logs
        logging.getLogger("azure").setLevel(logging.WARNING)
        logging.getLogger("azure.core").setLevel(logging.WARNING)
        logging.getLogger("azure.core.pipeline").setLevel(logging.WARNING)
        logging.getLogger("azure.core.pipeline.policies").setLevel(logging.WARNING)
        logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(
            logging.WARNING
        )
        logging.getLogger("azure.identity").setLevel(logging.WARNING)
        logging.getLogger("azure.identity._internal").setLevel(logging.WARNING)
        logging.getLogger("azure.ai.ml").setLevel(logging.WARNING)
        logging.getLogger("msrest").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("msal").setLevel(logging.WARNING)

    def _load_config(
        self,
        subscription_id: Optional[str],
        resource_group: Optional[str],
        workspace_name: Optional[str],
        compute_name: Optional[str],
    ):
        """Load configuration from environment variables or parameters."""
        # Load .env file if present
        env_path = find_dotenv(usecwd=True)
        if env_path:
            load_dotenv(env_path)
            self.logger.info("Loaded configuration from: %s", env_path)

        # Set configuration with priority: parameter > environment variable
        self.subscription_id = subscription_id or os.getenv("SUBSCRIPTION_ID")
        self.resource_group = resource_group or os.getenv("RESOURCE_GROUP")
        self.workspace_name = workspace_name or os.getenv("AML_WORKSPACE_NAME")
        self.compute_name = compute_name or os.getenv("AML_COMPUTE_NAME")

        # Validate required config
        missing = []
        if not self.subscription_id:
            missing.append("SUBSCRIPTION_ID")
        if not self.resource_group:
            missing.append("RESOURCE_GROUP")
        if not self.workspace_name:
            missing.append("AML_WORKSPACE_NAME")
        if not self.compute_name:
            missing.append("AML_COMPUTE_NAME")

        if missing:
            raise ValueError(
                f"Missing required configuration: {', '.join(missing)}\n"
                f"Please set these in your .env file or pass as parameters.\n"
                f"For AML_COMPUTE_NAME, this should be the name of your Azure ML compute instance or cluster.\n"
                f"You can create one in Azure ML Studio or add one to your Bicep deployment."
            )

    def _initialize_client(self):
        """Initialize Azure ML client."""
        self.logger.info("Connecting to Azure ML workspace...")
        credential = DefaultAzureCredential()
        self.ml_client = MLClient(
            credential=credential,
            subscription_id=self.subscription_id,
            resource_group_name=self.resource_group,
            workspace_name=self.workspace_name,
        )
        self.logger.info("Connected to workspace: %s", self.workspace_name)

    def _parse_pyproject(self) -> Dict[str, Any]:
        """
        Parse pyproject.toml to extract dependencies for environment creation.

        Returns:
            Dict with project metadata and dependencies
        """
        pyproject_path = Path.cwd() / "pyproject.toml"

        if not pyproject_path.exists():
            raise FileNotFoundError(f"pyproject.toml not found at {pyproject_path}")

        with open(pyproject_path, "rb") as f:
            pyproject = tomllib.load(f)

        return pyproject

    def _generate_environment_yml(
        self, pyproject: Dict[str, Any], output_path: Path
    ) -> None:
        """
        Generate environment.yml from pyproject.toml main dependencies.

        Args:
            pyproject: Parsed pyproject.toml data
            output_path: Path to write environment.yml
        """
        # Get all dependencies from main dependencies list
        all_deps = pyproject.get("project", {}).get("dependencies", [])

        # Exclude orchestration-only packages (not needed in compute environment)
        # Keep all ML/compute packages (torch, transformers, colpali, etc.)
        exclude_packages = ["model", "azure-ai-ml", "azure-identity", "python-dotenv"]

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

        # Create conda environment structure
        env_data = {
            "name": "aml_environment",
            "channels": ["conda-forge", "defaults"],
            "dependencies": [
                f"python={pyproject.get('project', {}).get('requires-python', '>=3.12').replace('>=', '')}",
                "pip",
                {"pip": compute_deps},
            ],
        }

        # Write environment.yml
        with open(output_path, "w") as f:
            yaml.dump(env_data, f, default_flow_style=False, sort_keys=False)

        self.logger.info(
            "Generated environment.yml with %d dependencies", len(compute_deps)
        )

    def _ensure_environment_yml(self) -> Path:
        """
        Ensure environment.yml exists, generate from pyproject.toml if needed.

        Returns:
            Path to environment.yml
        """
        env_yml_path = Path.cwd() / "environment.yml"

        # Always regenerate to ensure it's up to date
        self.logger.info(
            "Generating environment.yml from %s", Path.cwd() / "pyproject.toml"
        )
        pyproject = self._parse_pyproject()
        self._generate_environment_yml(pyproject, env_yml_path)

        return env_yml_path

    def _create_environment(self) -> Environment:
        """
        Create or get Azure ML environment.

        Returns:
            Azure ML Environment object
        """
        env_path = self._ensure_environment_yml()

        # Create environment name for ColQwen2
        env_name = "aml-colqwen2-env"

        self.logger.info("Creating Azure ML environment: %s", env_name)
        env = Environment(
            name=env_name,
            description="Environment for ColQwen2 model download and processing",
            conda_file=str(env_path),
            image="mcr.microsoft.com/azureml/openmpi4.1.0-cuda11.8-cudnn8-ubuntu22.04:latest",
        )

        # Register environment
        env = self.ml_client.environments.create_or_update(env)
        self.logger.info(
            "Environment registered: %s (version %s)", env.name, env.version
        )
        return env

    def _ensure_compute_running(self):
        """Ensure compute instance is running."""
        self.logger.info("Checking compute status: %s", self.compute_name)

        try:
            compute = self.ml_client.compute.get(self.compute_name)
            self.logger.info("Compute state: %s", compute.provisioning_state)

            if compute.provisioning_state not in ["Succeeded", "Running"]:
                self.logger.warning(
                    "Compute is in state: %s", compute.provisioning_state
                )
        except Exception as e:
            self.logger.error("Failed to get compute status: %s", e)
            raise

    def sanitize_model_name(self, model_name: str) -> str:
        """
        Sanitize model name for Azure ML (alphanumeric, dashes, underscores only).

        Args:
            model_name: Original model name (e.g., 'colqwen2-v1.0')

        Returns:
            Sanitized name (e.g., 'colqwen2-v1-0')
        """
        safe_name = model_name.replace("/", "-").replace(":", "-").replace("@", "-")
        safe_name = "".join(
            c if c.isalnum() or c in ["-", "_", "."] else "-" for c in safe_name
        )
        return safe_name

    def register_model(
        self,
        pipeline_job,
        model_name: str,
        model_type: str,
        tags: Dict[str, str],
        description: str = None,
        output_key: str = None,
    ) -> Model:
        """
        Register a model to Azure ML workspace with consistent naming.

        Args:
            pipeline_job: Completed pipeline job with outputs
            model_name: Original model name (e.g., 'colqwen2-v1.0')
            model_type: Type suffix (e.g., 'colqwen2-model')
            tags: Additional metadata tags
            description: Model description
            output_key: Key to use for pipeline output (defaults to f"{model_type}_model")

        Returns:
            Registered Model object
        """
        # Sanitize and create AML model name with consistent format
        safe_model_name = self.sanitize_model_name(model_name)
        aml_model_name = f"{safe_model_name}-{model_type}"

        # Add common tags
        all_tags = {
            "original_model": model_name,
            "pipeline_type": self.get_pipeline_type(),
            **tags,
        }

        try:
            # Get the output path from the pipeline job
            if output_key is None:
                output_key = f"{model_type}_model"
            output_path = pipeline_job.outputs[output_key].path

            # Create model entity
            model = Model(
                path=output_path,
                name=aml_model_name,
                description=description
                or f"{model_type.title()} model from {model_name}",
                type=AssetTypes.CUSTOM_MODEL,
                tags=all_tags,
            )

            # Register model
            registered_model = self.ml_client.models.create_or_update(model)
            self.logger.info("Model registered successfully!")
            self.logger.info("Name: %s", registered_model.name)
            self.logger.info("Version: %s", registered_model.version)
            self.logger.info("ID: %s", registered_model.id)
            self.logger.info("Tags: %s", all_tags)

            return registered_model

        except Exception as e:
            self.logger.error("Failed to register model: %s", e)
            self.logger.warning(
                "Model artifacts are available at: %s",
                pipeline_job.outputs.get(f"{model_type}_model"),
            )
            raise

    def _create_component(self, env: Environment):
        """Create the model download component."""
        self.logger.info("Creating ColQwen2 download component...")

        component = command(
            name="download_colqwen2",
            display_name="Download ColQwen2 Model",
            description="Download ColQwen2 base model and adapter for offline use",
            inputs={},
            outputs={
                "colqwen2_model": Output(type="custom_model", mode="rw_mount"),
            },
            code="./scripts",
            command=(
                "python download_model.py --output_dir ${{outputs.colqwen2_model}}"
            ),
            environment=f"{env.name}:{env.version}",
        )

        self.logger.info("ColQwen2 download component created successfully")
        return component

    def _define_pipeline(self):
        """Define the ColQwen2 registration pipeline structure."""

        env = self._create_environment()
        download_component = self._create_component(env)

        @dsl.pipeline(
            compute=self.compute_name,
            description="Pipeline to download and register ColQwen2 model",
        )
        def colqwen2_pipeline():
            # Download model
            download_step = download_component()

            # Set name and version on the node output to register it as a model
            download_step.outputs.colqwen2_model.name = "colqwen2-model"
            download_step.outputs.colqwen2_model.version = "1"

            return {
                "colqwen2_model": download_step.outputs.colqwen2_model,
            }

        return colqwen2_pipeline()

    def get_pipeline_type(self) -> str:
        """Return the pipeline type identifier."""
        return "colqwen2"

    def create_experiment_name(self) -> str:
        """Create experiment name for the pipeline."""
        return "colqwen2-model-registration"

    def run(self, wait_for_completion: bool = True):
        """Run the ColQwen2 model registration pipeline."""
        experiment_name = self.create_experiment_name()

        self.logger.info("=" * 60)
        self.logger.info("Running ColQwen2 Model Registration Pipeline")
        self.logger.info("=" * 60)
        self.logger.info("Experiment: %s", experiment_name)
        self.logger.info("Compute: %s", self.compute_name)
        self.logger.info("=" * 60)

        self._ensure_compute_running()

        self.logger.info("Instantiating pipeline...")
        pipeline = self._define_pipeline()

        self.logger.info("Submitting pipeline job to Azure ML...")
        pipeline_job = self.ml_client.jobs.create_or_update(
            pipeline, experiment_name=experiment_name
        )

        self.logger.info("Pipeline submitted successfully!")
        self.logger.info("Job name: %s", pipeline_job.name)
        self.logger.info("Studio URL: %s", pipeline_job.studio_url)

        if wait_for_completion:
            self.logger.info("Streaming pipeline logs...")
            self.ml_client.jobs.stream(pipeline_job.name)

            self.logger.info("Pipeline completed successfully!")
            self.logger.info(
                "Model registered to Azure ML workspace as part of pipeline"
            )

        return pipeline_job


def main():
    """CLI entry point for the ColQwen2 registration pipeline."""
    parser = argparse.ArgumentParser(description="Register ColQwen2 model on Azure ML")
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Don't wait for pipeline completion (submit and exit)",
    )

    args = parser.parse_args()

    # Create and run pipeline
    pipeline = ColpaliRegistrationPipeline()
    pipeline.run(wait_for_completion=not args.no_wait)


if __name__ == "__main__":
    main()
