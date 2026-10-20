# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from document_processor.document_processor import DocumentProcessor


class MessageHandlingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.processor = DocumentProcessor.__new__(DocumentProcessor)
        self.processor._process_service_bus_message = AsyncMock()
        self.message = SimpleNamespace(message_id="test-message", delivery_count=0)
        self.receiver = SimpleNamespace(
            complete_message=AsyncMock(),
            abandon_message=AsyncMock(),
            dead_letter_message=AsyncMock(),
        )

    async def process_message(self):
        await self.processor._process_service_bus_message_safe(
            self.message, self.receiver
        )

    async def test_successful_handler_completes_message(self):
        await self.process_message()
        self.processor._process_service_bus_message.assert_awaited_once_with(
            self.message
        )
        self.receiver.complete_message.assert_awaited_once_with(self.message)
        self.receiver.abandon_message.assert_not_awaited()
        self.receiver.dead_letter_message.assert_not_awaited()

    async def test_handler_exception_abandons_message_for_retry(self):
        self.processor._process_service_bus_message.side_effect = RuntimeError("failed")
        await self.process_message()
        self.receiver.complete_message.assert_not_awaited()
        self.receiver.abandon_message.assert_awaited_once_with(self.message)
        self.receiver.dead_letter_message.assert_not_awaited()

    async def test_final_failure_dead_letters_message(self):
        self.message.delivery_count = 2
        self.processor._process_service_bus_message.side_effect = RuntimeError("failed")
        await self.process_message()
        self.receiver.complete_message.assert_not_awaited()
        self.receiver.abandon_message.assert_not_awaited()
        self.receiver.dead_letter_message.assert_awaited_once_with(
            self.message,
            reason="MaxDeliveryCountExceeded",
            error_description="Failed after 2 attempts. Last error: failed",
        )

    async def test_failed_dead_letter_falls_back_to_abandon(self):
        self.message.delivery_count = 2
        self.processor._process_service_bus_message.side_effect = RuntimeError("failed")
        self.receiver.dead_letter_message.side_effect = RuntimeError("unavailable")
        await self.process_message()
        self.receiver.complete_message.assert_not_awaited()
        self.receiver.dead_letter_message.assert_awaited_once()
        self.receiver.abandon_message.assert_awaited_once_with(self.message)
