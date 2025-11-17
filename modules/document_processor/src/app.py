# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""
Document Processor Service - Service Bus Consumer
Processes documents using ColPali and indexes in Qdrant
Consumes messages from Azure Service Bus for reliable 1-in-1-out processing
"""

import asyncio
import logging
import os
import signal
import sys

from dotenv import find_dotenv, load_dotenv

from .document_processor import DocumentProcessor
from .setup_logging import configure_telemetry, trace_operation

# Find and load .env file from the project root
load_dotenv(find_dotenv())

configure_telemetry()

logger = logging.getLogger(__name__)

# Configure Azure Monitor OpenTelemetry for Application Insights
try:
    from azure.monitor.opentelemetry import configure_azure_monitor

    app_insights_connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")

    if app_insights_connection_string:
        configure_azure_monitor(
            connection_string=app_insights_connection_string,
        )
        logger.info("Application Insights telemetry configured successfully")
    else:
        logger.warning(
            "APPLICATIONINSIGHTS_CONNECTION_STRING not found - Application Insights telemetry disabled"
        )

except ImportError:
    logger.warning(
        "azure-monitor-opentelemetry not available - Application Insights telemetry disabled"
    )
except Exception as e:
    logger.error("Failed to configure Application Insights telemetry: %s", e)


class GracefulShutdown:
    """Handle graceful shutdown for the document processor."""

    def __init__(self):
        self.shutdown = False
        self.processor = None

    def exit_gracefully(self, signum, frame):
        logger.info(
            "Received shutdown signal %s, initiating graceful shutdown...", signum
        )
        self.shutdown = True


@trace_operation(operation_name="consumer_mode", new_root=True)
async def consumer_mode():
    """Run the Service Bus consumer for document processing."""
    shutdown_handler = GracefulShutdown()
    processor = DocumentProcessor()
    shutdown_handler.processor = processor

    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, shutdown_handler.exit_gracefully)
    signal.signal(signal.SIGINT, shutdown_handler.exit_gracefully)

    try:
        logger.info("Starting standalone Service Bus consumer...")
        await processor.initialize_service_bus()
        await processor.start_message_consumption()
    except KeyboardInterrupt:
        logger.info("Consumer stopped by user")
    except Exception as e:
        logger.error("Consumer error: %s", e)
        raise
    finally:
        if shutdown_handler.shutdown:
            logger.info("Graceful shutdown completed")
            sys.exit(0)


def main():
    """Main entry point for running the Service Bus consumer directly"""
    logger.info("Document Processor starting as standalone Service Bus consumer")
    asyncio.run(consumer_mode())


if __name__ == "__main__":
    main()
