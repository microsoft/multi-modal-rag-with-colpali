"""
Logging and telemetry configuration for Document Processor service.
Configures Python logging and Azure Monitor OpenTelemetry integration.
"""

import logging
import os
import uuid

from opentelemetry import trace
from opentelemetry.context import Context

# Configure basic logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)

# Suppress verbose Azure SDK logging
# Must set propagate=False to prevent inheriting root logger's INFO level
azure_http_logger = logging.getLogger(
    "azure.core.pipeline.policies.http_logging_policy"
)
azure_http_logger.setLevel(logging.WARNING)
azure_http_logger.propagate = False

azure_exporter_logger = logging.getLogger(
    "azure.monitor.opentelemetry.exporter.export._base"
)
azure_exporter_logger.setLevel(logging.WARNING)
azure_exporter_logger.propagate = False

# Service Bus loggers - just set level, no need to disable propagation
logging.getLogger("azure.servicebus").setLevel(logging.WARNING)
logging.getLogger("azure.servicebus.aio").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def configure_telemetry():
    """Configure Azure Monitor OpenTelemetry for Application Insights"""
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        # Get Application Insights connection string from environment
        app_insights_connection_string = os.getenv(
            "APPLICATIONINSIGHTS_CONNECTION_STRING"
        )

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


def trace_operation(operation_name=None, new_root=True):
    """
    Decorator to create a trace span for a function.
    Supports both sync and async functions.

    Args:
        operation_name: Custom name for the span (defaults to function name)
        new_root: If True, creates a new root trace context with unique operation ID.
                  If False, creates a child span within current context.
    """

    def decorator(func):
        import asyncio

        async def async_wrapper(*args, **kwargs):
            tracer = trace.get_tracer(__name__)
            span_name = operation_name or func.__name__

            if new_root:
                # Create a new root span with unique operation ID
                with tracer.start_as_current_span(
                    span_name,
                    context=Context(),
                    attributes={"operation.id": str(uuid.uuid4())},
                ):
                    return await func(*args, **kwargs)
            else:
                # Create a child span within current context
                with tracer.start_as_current_span(span_name):
                    return await func(*args, **kwargs)

        def sync_wrapper(*args, **kwargs):
            tracer = trace.get_tracer(__name__)
            span_name = operation_name or func.__name__

            if new_root:
                # Create a new root span with unique operation ID
                with tracer.start_as_current_span(
                    span_name,
                    context=Context(),
                    attributes={"operation.id": str(uuid.uuid4())},
                ):
                    return func(*args, **kwargs)
            else:
                # Create a child span within current context
                with tracer.start_as_current_span(span_name):
                    return func(*args, **kwargs)

        # Return appropriate wrapper based on function type
        if asyncio.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator
