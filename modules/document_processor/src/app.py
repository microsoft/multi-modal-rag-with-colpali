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

from .document_processor import DocumentProcessor
from .logging import configure_telemetry, trace_operation

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
    logger.error(f"Failed to configure Application Insights telemetry: {e}")


@trace_operation(operation_name="consumer_mode", new_root=True)
async def consumer_mode():
    """Run the Service Bus consumer for document processing."""
    processor = DocumentProcessor()

    try:
        logger.info("Starting standalone Service Bus consumer...")
        await processor.initialize_service_bus()
        await processor.start_message_consumption()
    except KeyboardInterrupt:
        logger.info("Consumer stopped by user")
    except Exception as e:
        logger.error(f"Consumer error: {e}")
        raise


def main():
    """Main entry point for running the Service Bus consumer directly"""
    logger.info("Document Processor starting as standalone Service Bus consumer")
    asyncio.run(consumer_mode())


if __name__ == "__main__":
    main()
