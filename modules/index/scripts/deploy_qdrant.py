#!/usr/bin/env python3
"""
ColPali QDRANT Collection Deployment Script

This script creates and configures a QDRANT collection optimized for ColPali visual document
embeddings. The collection uses multiple vector configurations for different embedding strategies
and stores the same document metadata as the AI Search index.

QDRANT Collection Configuration:
- Document metadata fields: id, source_file, upload_timestamp, page_number, page_image_base64, processing_timestamp
- Vector spaces: original (full embeddings), pooled (hierarchically pooled embeddings) (both 128D, cosine)
- Search strategy: Multi-vector similarity with cosine distance and MAX_SIM comparator

Features:
- Creates QDRANT collection with multi-vector support
- Configures 2 vector spaces: original, pooled
- Uses cosine similarity with MAX_SIM comparator
- Optimized for ColPali hierarchical pooling (128 dimensions)
- Stores document metadata for retrieval and display

Usage:
    python deploy_qdrant.py    # Create or update the QDRANT collection

Configuration:
    Reads QDRANT_ENDPOINT and QDRANT_COLLECTION_NAME from .env file in project root.
    Requires QDRANT container app to be deployed and running.
"""

import logging
import os
import sys
from typing import Dict

from dotenv import find_dotenv, load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.http import models


class ColPaliQdrantDeployer:
    """Deploys and manages ColPali QDRANT collection."""

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
        self.qdrant_endpoint = os.getenv("QDRANT_ENDPOINT")
        self.collection_name = os.getenv("QDRANT_COLLECTION_NAME", "colpali-documents")

        if not self.qdrant_endpoint:
            self.logger.error("QDRANT_ENDPOINT not found in environment variables")
            self.logger.error(
                "Please ensure the container apps are deployed and .env file is properly configured"
            )
            sys.exit(1)

        self.logger.info("QDRANT Endpoint: %s", self.qdrant_endpoint)
        self.logger.info("Collection Name: %s", self.collection_name)

        # Initialize QDRANT client with extended timeout
        try:
            self.client = QdrantClient(url=self.qdrant_endpoint, port=443, timeout=120)
            self.logger.info("Successfully connected to QDRANT")
        except Exception as e:
            self.logger.error("Failed to connect to QDRANT: %s", str(e))
            sys.exit(1)

    def collection_exists(self) -> bool:
        """Check if the collection already exists."""
        try:
            collections = self.client.get_collections()
            return any(
                collection.name == self.collection_name
                for collection in collections.collections
            )
        except Exception as e:
            self.logger.error("Failed to check if collection exists: %s", str(e))
            return False

    def create_collection_schema(self) -> Dict[str, models.VectorParams]:
        """
        Create the QDRANT collection schema for ColPali documents.

        Creates vector configurations that match the AI Search index structure:
        - original: Direct patch embeddings (HNSW disabled for exact search)
        - mean_pooling_columns: Column-wise pooled embeddings
        - mean_pooling_rows: Row-wise pooled embeddings

        Payload schema mirrors AI Search fields:
        - id: Document identifier (key)
        - source_file: Original filename
        - upload_timestamp: When document was uploaded
        - page_number: Page number within document
        - page_image_base64: Base64 encoded page image
        - processing_timestamp: When document was processed

        Returns:
            Dict of vector configurations for QDRANT collection
        """
        self.logger.info(
            "Creating ColPali collection schema with 2 vector spaces: 'original' and 'pooled'..."
        )

        vectors_config = {
            "original": models.VectorParams(
                size=128,
                distance=models.Distance.COSINE,
                multivector_config=models.MultiVectorConfig(
                    comparator=models.MultiVectorComparator.MAX_SIM
                ),
                hnsw_config=models.HnswConfigDiff(
                    m=0  # switching off HNSW for exact search
                ),
            ),
            "pooled": models.VectorParams(
                size=128,
                distance=models.Distance.COSINE,
                multivector_config=models.MultiVectorConfig(
                    comparator=models.MultiVectorComparator.MAX_SIM
                ),
            ),
        }

        return vectors_config

    def create_collection(self) -> bool:
        """Create the ColPali QDRANT collection with optimized vector configurations."""
        try:
            self.logger.info("Creating QDRANT collection: %s", self.collection_name)

            # Get collection schema
            vectors_config = self.create_collection_schema()

            # Create collection with multiple vector configurations for ColPali
            self.client.create_collection(
                collection_name=self.collection_name, vectors_config=vectors_config
            )

            self.logger.info("QDRANT collection created successfully")
            return True

        except Exception as e:
            self.logger.error("Failed to create QDRANT collection: %s", str(e))
            return False

    def verify_collection(self) -> bool:
        """Verify the collection was created correctly."""
        try:
            collection_info = self.client.get_collection(self.collection_name)
            self.logger.info("Collection verification:")
            self.logger.info("  Status: %s", collection_info.status)
            self.logger.info("  Points count: %d", collection_info.points_count)
            self.logger.info(
                "  Vectors config: %d vector spaces",
                len(collection_info.config.params.vectors),
            )

            # Verify vector configurations
            vectors = collection_info.config.params.vectors
            expected_vectors = ["original", "pooled"]

            for vector_name in expected_vectors:
                if vector_name in vectors:
                    vector_config = vectors[vector_name]
                    self.logger.info(
                        "  %s: size=%d, distance=%s, multivector=MAX_SIM",
                        vector_name,
                        vector_config.size,
                        vector_config.distance,
                    )
                else:
                    self.logger.warning(
                        "  Missing vector configuration: %s", vector_name
                    )

            return True

        except Exception as e:
            self.logger.error("Failed to verify collection: %s", str(e))
            return False

    def deploy_collection(self) -> bool:
        """
        Deploy (create or update) the ColPali QDRANT collection.

        Returns:
            True if successful, False otherwise
        """
        try:
            self.logger.info("Deploying QDRANT collection: %s", self.collection_name)

            # Check if collection already exists
            if self.collection_exists():
                self.logger.info(
                    "Collection '%s' already exists - recreating it",
                    self.collection_name,
                )
                try:
                    self.client.delete_collection(self.collection_name)
                    self.logger.info("Existing collection deleted")
                except Exception as e:
                    self.logger.error(
                        "Failed to delete existing collection: %s", str(e)
                    )
                    return False

            # Create the collection
            if not self.create_collection():
                return False

            # Verify the collection
            if not self.verify_collection():
                return False

            self.logger.info(
                "Successfully deployed collection: %s", self.collection_name
            )
            return True

        except Exception as e:
            self.logger.error("Failed to deploy collection: %s", str(e))
            return False


def main():
    """Main entry point for the deployment script."""
    # Initialize deployer and deploy the collection
    deployer = ColPaliQdrantDeployer()
    success = deployer.deploy_collection()

    if success:
        deployer.logger.info("=" * 60)
        deployer.logger.info("QDRANT Collection Deployment Summary")
        deployer.logger.info("=" * 60)
        deployer.logger.info("Collection Name: %s", deployer.collection_name)
        deployer.logger.info("QDRANT Endpoint: %s", deployer.qdrant_endpoint)
        deployer.logger.info("Vector Configurations:")
        deployer.logger.info("   - original (128D, COSINE, MAX_SIM, HNSW disabled)")
        deployer.logger.info("   - mean_pooling_columns (128D, COSINE, MAX_SIM)")
        deployer.logger.info("   - mean_pooling_rows (128D, COSINE, MAX_SIM)")
        deployer.logger.info("Payload Schema:")
        deployer.logger.info("   - id: Document identifier")
        deployer.logger.info("   - source_file: Original filename")
        deployer.logger.info("   - upload_timestamp: Upload time")
        deployer.logger.info("   - page_number: Page within document")
        deployer.logger.info("   - page_image_base64: Base64 page image")
        deployer.logger.info("   - processing_timestamp: Processing time")
        deployer.logger.info("QDRANT collection is ready for ColPali embeddings")

    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
