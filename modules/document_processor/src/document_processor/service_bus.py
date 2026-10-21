# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Single-queue consumer with deadlines, rolling slots, and explicit settlement."""

import asyncio
import logging
import math
import os

from azure.servicebus.aio import AutoLockRenewer
from azure.servicebus.exceptions import ServiceBusError

logger = logging.getLogger(__name__)


class CleanupError(RuntimeError):
    pass


def positive_setting(name: str, default: float) -> float:
    value = float(os.getenv(name, str(default)))
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return value


async def drain(tasks, timeout: float):
    tasks = set(tasks)
    for task in tasks:
        if not task.done():
            task.cancel()
    if not tasks:
        return
    deadline = asyncio.get_running_loop().time() + timeout
    cancelled = False
    pending = tasks
    while pending:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            for task in pending:
                task.add_done_callback(
                    lambda done: None if done.cancelled() else done.exception()
                )
            raise CleanupError(
                "Cancellation did not finish; refusing to reuse processing slots"
            )
        try:
            _, pending = await asyncio.wait(pending, timeout=remaining)
        except asyncio.CancelledError:
            cancelled = True
    for task in tasks:
        if not task.cancelled():
            task.exception()
    if cancelled:
        raise asyncio.CancelledError


async def bounded(awaitable, timeout: float, cancellation_timeout: float = 5):
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        await drain({task}, cancellation_timeout)
        raise
    if not done:
        await drain({task}, cancellation_timeout)
        raise TimeoutError("Operation exceeded its deadline")
    return task.result()


class ServiceBusConsumer:
    def __init__(self, client, queue_name, handler, shutdown_event):
        self.client, self.queue_name, self.handler = client, queue_name, handler
        self.shutdown_event = shutdown_event
        self.operation_timeout = positive_setting(
            "SERVICE_BUS_OPERATION_TIMEOUT_SECONDS", 30
        )
        self.cancel_timeout = positive_setting(
            "SERVICE_BUS_CANCELLATION_TIMEOUT_SECONDS", 5
        )
        self.handler_timeout = positive_setting("DOCUMENT_TIMEOUT_SECONDS", 1200)
        self.concurrency = int(os.getenv("DOCUMENT_MAX_CONCURRENT", "3"))
        self.poison_threshold = int(
            os.getenv("SERVICE_BUS_POISON_DELIVERY_THRESHOLD", "10")
        )
        if min(self.concurrency, self.poison_threshold) <= 0:
            raise ValueError(
                "Document concurrency and poison threshold must be positive integers"
            )

    async def _bounded(self, operation, timeout=None):
        return await bounded(
            operation, timeout or self.operation_timeout, self.cancel_timeout
        )

    async def _settle(self, receiver, message, operation, **kwargs):
        if getattr(message, "auto_renew_error", None):
            logger.warning("Lock renewal failed; leaving message unsettled")
            return
        try:
            await self._bounded(
                getattr(receiver, f"{operation}_message")(message, **kwargs)
            )
        except CleanupError:
            raise
        except (ServiceBusError, TimeoutError):
            logger.warning(
                "Message settlement failed; leaving it for redelivery", exc_info=True
            )

    async def process_message(self, receiver, message):
        settlement_started = False
        try:
            if (message.delivery_count or 0) > self.poison_threshold:
                settlement_started = True
                await self._settle(
                    receiver,
                    message,
                    "dead_letter",
                    reason="PoisonMessage",
                    error_description="Repeated unsettled deliveries; inspect document before replay",
                )
                return
            result = await self._bounded(self.handler(message), self.handler_timeout)
            settlement_started = True
            if result.success:
                await self._settle(receiver, message, "complete")
            elif result.dead_letter_reason or result.permanent_failure:
                await self._settle(
                    receiver,
                    message,
                    "dead_letter",
                    reason=result.dead_letter_reason or "InvalidDocument",
                    error_description=(result.error_message or "Invalid document")[
                        :2048
                    ],
                )
            else:
                await self._settle(receiver, message, "abandon")
        except CleanupError:
            raise
        except asyncio.CancelledError:
            if not settlement_started:
                await self._settle(receiver, message, "abandon")
            raise
        except Exception:
            logger.exception("Message processing failed")
            if not settlement_started:
                await self._settle(receiver, message, "abandon")

    async def listen(self):
        while not self.shutdown_event.is_set():
            receiver = self.client.get_queue_receiver(
                queue_name=self.queue_name, prefetch_count=0
            )
            renewer = AutoLockRenewer(
                max_lock_renewal_duration=self.handler_timeout + 60
            )
            tasks = set()
            try:
                await self._bounded(receiver.__aenter__())
                while not self.shutdown_event.is_set():
                    done = {task for task in tasks if task.done()}
                    tasks -= done
                    if done:
                        outcomes = await asyncio.gather(*done, return_exceptions=True)
                        for outcome in outcomes:
                            if isinstance(outcome, BaseException):
                                raise outcome
                    slots = self.concurrency - len(tasks)
                    if slots:
                        messages = await self._bounded(
                            receiver.receive_messages(
                                max_message_count=slots,
                                max_wait_time=1,
                            )
                        )
                        for message in messages:
                            renewer.register(receiver, message)
                            tasks.add(
                                asyncio.create_task(
                                    self.process_message(receiver, message)
                                )
                            )
                    else:
                        await asyncio.wait(
                            tasks, timeout=0.5, return_when=asyncio.FIRST_COMPLETED
                        )
                if tasks:
                    await asyncio.wait(tasks, timeout=self.operation_timeout)
            except CleanupError:
                raise
            except (ServiceBusError, TimeoutError, OSError):
                logger.warning(
                    "Service Bus connection failed; reconnecting", exc_info=True
                )
            finally:
                try:
                    await drain(tasks, self.cancel_timeout)
                finally:
                    errors = []
                    for resource in (receiver, renewer):
                        try:
                            await self._bounded(resource.close())
                        except (Exception, asyncio.CancelledError) as error:
                            logger.error("Service Bus cleanup failed: %s", error)
                            errors.append(error)
                    if errors:
                        raise CleanupError(
                            "Service Bus resource cleanup failed"
                        ) from errors[0]
            if not self.shutdown_event.is_set():
                try:
                    await asyncio.wait_for(self.shutdown_event.wait(), timeout=1)
                except TimeoutError:
                    pass
