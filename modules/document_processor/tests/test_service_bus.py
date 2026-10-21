# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from azure.servicebus.exceptions import ServiceBusError

from document_processor.models import ProcessingResult
from document_processor.service_bus import CleanupError, ServiceBusConsumer, bounded


@pytest.fixture
def consumer():
    return ServiceBusConsumer(None, "documents", AsyncMock(), asyncio.Event())


def message(deliveries=0):
    return SimpleNamespace(delivery_count=deliveries, auto_renew_error=None)


async def test_poison_checked_before_handler(consumer):
    receiver = SimpleNamespace(
        dead_letter_message=AsyncMock(), complete_message=AsyncMock()
    )
    await consumer.process_message(receiver, message(consumer.poison_threshold + 1))
    receiver.dead_letter_message.assert_awaited_once()
    consumer.handler.assert_not_awaited()
    receiver.complete_message.assert_not_awaited()


async def test_operation_deadline_cancels_and_drains():
    cleaned = asyncio.Event()

    async def operation():
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    with pytest.raises(TimeoutError):
        await bounded(operation(), 0.01)
    assert cleaned.is_set()


async def test_stuck_cancellation_fails_closed():
    release = asyncio.Event()

    async def operation():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(operation())
    try:
        with pytest.raises(CleanupError):
            await bounded(task, 0.01, 0.01)
    finally:
        release.set()
        await task


async def test_settlement_timeout_does_not_attempt_second_settlement(consumer):
    consumer.operation_timeout = 0.01
    receiver = SimpleNamespace(
        complete_message=AsyncMock(),
        abandon_message=AsyncMock(),
    )

    async def stall(_):
        await asyncio.Event().wait()

    receiver.complete_message.side_effect = stall
    consumer.handler.return_value = ProcessingResult(success=True, document_id="test")
    await consumer.process_message(receiver, message())
    receiver.complete_message.assert_awaited_once()
    receiver.abandon_message.assert_not_awaited()


async def test_lost_lock_is_not_settled(consumer):
    receiver = SimpleNamespace(complete_message=AsyncMock())
    consumer.handler.return_value = ProcessingResult(success=True, document_id="test")
    item = message()
    item.auto_renew_error = RuntimeError("lock lost")
    await consumer.process_message(receiver, item)
    receiver.complete_message.assert_not_awaited()


async def test_listener_refills_slots_before_slow_document_finishes(monkeypatch):
    shutdown, release = asyncio.Event(), asyncio.Event()
    handled = []
    first, second, third = message(), message(), message()
    batches = [[first, second], [third]]

    async def receive(**kwargs):
        return batches.pop(0) if batches else []

    receiver = SimpleNamespace(
        __aenter__=AsyncMock(),
        close=AsyncMock(),
        receive_messages=AsyncMock(side_effect=receive),
        complete_message=AsyncMock(),
        abandon_message=AsyncMock(),
    )
    renewer = SimpleNamespace(register=Mock(), close=AsyncMock())
    monkeypatch.setattr(
        "document_processor.service_bus.AutoLockRenewer", lambda **_: renewer
    )

    async def handler(item):
        handled.append(item)
        if item is first:
            await release.wait()
        if item is third:
            release.set()
            shutdown.set()
        return ProcessingResult(success=True, document_id="test")

    consumer = ServiceBusConsumer(
        SimpleNamespace(get_queue_receiver=lambda **_: receiver),
        "documents",
        handler,
        shutdown,
    )
    consumer.concurrency = 2
    await asyncio.wait_for(consumer.listen(), timeout=5)
    assert len(handled) == 3
    assert receiver.complete_message.await_count == 3
    assert receiver.receive_messages.call_args_list[1].kwargs["max_message_count"] == 1
    receiver.close.assert_awaited_once()
    renewer.close.assert_awaited_once()


async def test_receive_deadline_reconnects_with_old_resources_closed(monkeypatch):
    shutdown = asyncio.Event()

    async def stall(**_):
        await asyncio.Event().wait()

    first = SimpleNamespace(
        __aenter__=AsyncMock(),
        close=AsyncMock(),
        receive_messages=AsyncMock(side_effect=stall),
    )

    async def receive(**_):
        assert first.close.await_count == 1
        shutdown.set()
        return []

    second = SimpleNamespace(
        __aenter__=AsyncMock(),
        close=AsyncMock(),
        receive_messages=AsyncMock(side_effect=receive),
    )
    receivers = [first, second]
    monkeypatch.setattr(
        "document_processor.service_bus.AutoLockRenewer",
        lambda **_: SimpleNamespace(register=Mock(), close=AsyncMock()),
    )
    consumer = ServiceBusConsumer(
        SimpleNamespace(get_queue_receiver=lambda **_: receivers.pop(0)),
        "documents",
        AsyncMock(),
        shutdown,
    )
    consumer.operation_timeout = 0.01
    await asyncio.wait_for(consumer.listen(), timeout=5)
    second.close.assert_awaited_once()


async def test_receive_failure_cannot_reconnect_after_handler_cleanup_failed(
    monkeypatch,
):
    release, worker_finished = asyncio.Event(), asyncio.Event()
    shutdown = asyncio.Event()
    handlers = []

    async def handler(_):
        handlers.append(asyncio.current_task())
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return ProcessingResult(success=True, document_id="test")

    class ObservedConsumer(ServiceBusConsumer):
        async def process_message(self, receiver, item):
            try:
                return await super().process_message(receiver, item)
            finally:
                worker_finished.set()

    receives = 0

    async def receive(**_):
        nonlocal receives
        receives += 1
        if receives == 1:
            return [message()]
        await worker_finished.wait()
        raise ServiceBusError("Receive failed after handler cleanup timed out")

    receiver = SimpleNamespace(
        __aenter__=AsyncMock(),
        close=AsyncMock(),
        receive_messages=AsyncMock(side_effect=receive),
        complete_message=AsyncMock(),
        abandon_message=AsyncMock(),
    )
    renewer = SimpleNamespace(register=Mock(), close=AsyncMock())
    monkeypatch.setattr(
        "document_processor.service_bus.AutoLockRenewer", lambda **_: renewer
    )
    connections = 0

    def connect(**_):
        nonlocal connections
        connections += 1
        if connections > 1:
            shutdown.set()
        return receiver

    consumer = ObservedConsumer(
        SimpleNamespace(get_queue_receiver=connect), "documents", handler, shutdown
    )
    consumer.concurrency = 2
    consumer.handler_timeout = consumer.cancel_timeout = 0.01
    try:
        with pytest.raises(CleanupError, match="refusing to reuse"):
            await asyncio.wait_for(consumer.listen(), timeout=5)
        assert connections == 1
        assert not handlers[0].done()
        receiver.complete_message.assert_not_awaited()
        receiver.abandon_message.assert_not_awaited()
        receiver.close.assert_awaited_once()
        renewer.close.assert_awaited_once()
    finally:
        release.set()
        await asyncio.gather(*handlers)
