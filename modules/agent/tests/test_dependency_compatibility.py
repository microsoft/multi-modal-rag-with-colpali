# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from msal.oauth2cli.assertion import JwtAssertionCreator
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from agent import app as api


class ApiCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.agent = AsyncMock()

        async def events(**kwargs):
            yield {"type": "text_delta", "content": "Answer"}
            yield {"type": "done", "sources": [{"source_file": "sample.pdf"}]}

        self.agent.run_stream = events
        self.factory = patch.object(api, "ColPaliAgent", return_value=self.agent)
        self.factory.start()
        self.client = TestClient(api.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.factory.stop()
        self.agent.initialize.assert_awaited_once()
        self.agent.cleanup.assert_awaited_once()

    def test_health_and_openapi(self):
        self.assertEqual(self.client.get("/health").json()["status"], "healthy")
        schema = self.client.get("/openapi.json").json()
        self.assertIn("/chat/stream", schema["paths"])

    def test_chat_response_and_validation(self):
        response = self.client.post("/chat", json={"message": "Question"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"response": "Answer", "sources": [{"source_file": "sample.pdf"}]},
        )
        self.assertEqual(self.client.post("/chat", json={}).status_code, 422)

    def test_sse_wire_format(self):
        response = self.client.post("/chat/stream", json={"message": "Question"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(
            response.headers["content-type"].startswith("text/event-stream")
        )
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        self.assertEqual([event["type"] for event in events], ["text_delta", "done"])
        self.assertEqual(events[0]["content"], "Answer")

    def test_cors_preflight(self):
        response = self.client.options(
            "/chat",
            headers={
                "Origin": "http://localhost:8080",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["access-control-allow-origin"], "http://localhost:8080"
        )


class DependencyCompatibilityTests(unittest.TestCase):
    def test_instrumented_fastapi_request(self):
        app = FastAPI()

        @app.get("/health")
        async def health():
            return {"status": "healthy"}

        FastAPIInstrumentor.instrument_app(app)
        try:
            with TestClient(app) as client:
                response = client.get("/health")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"status": "healthy"})
        finally:
            FastAPIInstrumentor.uninstrument_app(app)

    def test_msal_assertion_signing_with_cryptography(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        creator = JwtAssertionCreator(pem, algorithm="RS256")
        token = creator.create_normal_assertion(
            "https://example.com/token", "client-id"
        )
        claims = jwt.decode(
            token,
            key.public_key(),
            algorithms=["RS256"],
            audience="https://example.com/token",
        )
        self.assertEqual(claims["iss"], "client-id")
        self.assertEqual(claims["sub"], "client-id")

    def test_chainlit_ui_and_engineio_handshake(self):
        # Chainlit generates configuration on import; keep it out of the checkout.
        with tempfile.TemporaryDirectory() as directory:
            previous = os.getcwd()
            try:
                os.chdir(directory)
                from chainlit.server import app

                from agent import chainlit_ui

                self.assertTrue(callable(chainlit_ui.on_message))
                client = TestClient(app)
                self.assertEqual(client.get("/").status_code, 200)
                response = client.get("/ws/socket.io/?EIO=4&transport=polling")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.text.startswith("0"))
                self.assertIn("sid", json.loads(response.text[1:]))
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
