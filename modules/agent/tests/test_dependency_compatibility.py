# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import aiohttp
import fsspec
import httpx
import jwt
from agent_framework import ChatMessage, DataContent, Role, TextContent
from agent_framework.azure import AzureOpenAIChatClient
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from huggingface_hub import HfFileSystem
from msal.oauth2cli.assertion import JwtAssertionCreator
from openai.types.chat import ChatCompletion
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

with patch("agent.setup_logging.configure_telemetry"):
    from agent import app as api

from agent.agent import ColPaliAgent


class FrameworkCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_aiohttp_preserves_duplicate_headers(self):
        async def respond(request):
            self.assertEqual(request.headers.getall("X-Input"), ["one", "two"])
            return web.Response(
                text="ok", headers=[("X-Output", "first"), ("X-Output", "second")]
            )

        app = web.Application()
        app.router.add_get("/", respond)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"http://127.0.0.1:{port}/",
                    headers=[("X-Input", "one"), ("X-Input", "two")],
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(await response.text(), "ok")
                    self.assertEqual(
                        response.headers.getall("X-Output"), ["first", "second"]
                    )
        finally:
            await runner.cleanup()

    async def test_real_agent_uses_core_azure_chat_client(self):
        credential = Mock()
        qdrant = AsyncMock()
        environment = {
            "AI_FOUNDRY_OPEN_AI_ENDPOINT": "https://example.invalid",
            "COLPALI_INFERENCE_ENDPOINT": "https://example.invalid",
            "AZURE_OPENAI_API_KEY": "test-only",
            "AZURE_OPENAI_API_VERSION": "2024-10-21",
            "AZURE_CLIENT_ID": "",
        }
        with (
            patch.dict(os.environ, environment),
            patch("agent.agent.DefaultAzureCredential", return_value=credential),
            patch("agent.document_retriever.DefaultAzureCredential"),
            patch("agent.document_retriever.AsyncQdrantClient", return_value=qdrant),
            patch("agent.agent.os.makedirs"),
        ):
            agent = ColPaliAgent(deployment_name="test-model")
            await agent.initialize()
        self.assertIsInstance(agent._chat_client, AzureOpenAIChatClient)
        client = agent._chat_client
        response = ChatCompletion(
            id="test-completion",
            created=0,
            model="test-model",
            object="chat.completion",
            choices=[
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "Related question"},
                }
            ],
        )
        try:
            with patch.object(
                client.client.chat.completions,
                "create",
                new=AsyncMock(return_value=response),
            ) as complete:
                queries = await agent.generate_search_queries("Original question")
                self.assertEqual(queries, ["Original question", "Related question"])
                self.assertEqual(complete.call_args.kwargs["model"], "test-model")
                result = await client.get_response(
                    [
                        ChatMessage(
                            role=Role.USER,
                            contents=[
                                TextContent(text="Describe"),
                                DataContent(
                                    uri="data:image/png;base64,aGVsbG8=",
                                    media_type="image/png",
                                ),
                            ],
                        )
                    ]
                )
                self.assertEqual(result.text, "Related question")
                messages = complete.call_args.kwargs["messages"]
                self.assertEqual(messages[0]["content"][0]["text"], "Describe")
                self.assertEqual(
                    messages[1]["content"][0],
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,aGVsbG8="},
                    },
                )
        finally:
            await client.client.close()
            await agent.cleanup()
        credential.close.assert_called_once()
        qdrant.close.assert_awaited_once()


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
    def test_ui_server_serves_browser_assets_and_socket_handshake(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(api.__file__).resolve().parents[1])
            # Uvicorn inherits the reserved socket on Linux, as in the image.
            if sys.platform == "win32":
                listener.close()
                address = ["--host", "127.0.0.1", "--port", str(port)]
                process_options = {}
            else:
                address = ["--fd", str(listener.fileno())]
                process_options = {"pass_fds": (listener.fileno(),)}
            with tempfile.TemporaryFile(mode="w+") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "agent.ui_app:app",
                        "--loop",
                        "asyncio",
                        *address,
                    ],
                    cwd=directory,
                    env=environment,
                    stdout=log,
                    stderr=log,
                    **process_options,
                )
                try:
                    with httpx.Client(
                        base_url=f"http://127.0.0.1:{port}", timeout=2
                    ) as client:
                        deadline = time.monotonic() + 30
                        while True:
                            try:
                                response = client.get("/")
                                break
                            except httpx.TransportError:
                                if (
                                    process.poll() is not None
                                    or time.monotonic() > deadline
                                ):
                                    log.seek(0)
                                    self.fail(f"UI failed to start:\n{log.read()}")
                                time.sleep(0.1)
                        self.assertEqual(response.status_code, 200)
                        assets = re.findall(
                            r'(?:src|href)="([^"]+\.(?:js|css))"', response.text
                        )
                        self.assertTrue(
                            assets, "UI HTML must reference its application assets"
                        )
                        for asset in assets:
                            with self.subTest(asset=asset):
                                result = client.get(asset)
                                self.assertEqual(
                                    result.status_code, 200, result.text[:200]
                                )
                                self.assertTrue(result.content)
                        response = client.get("/ws/socket.io/?EIO=4&transport=polling")
                        self.assertEqual(response.status_code, 200)
                        self.assertIn("sid", json.loads(response.text[1:]))
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)

    def test_fsspec_reference_and_huggingface_filesystems(self):
        references = fsspec.filesystem(
            "reference", fo={"version": 1, "refs": {"document": "sample text"}}
        )
        self.assertEqual(references.cat_file("document"), b"sample text")
        self.assertIsInstance(HfFileSystem(token=False), fsspec.AbstractFileSystem)

    def test_only_core_framework_distribution_is_required(self):
        core = distribution("agent-framework-core")
        self.assertEqual(core.version, "1.0.0b251120")
        self.assertIn(
            "agent_framework/azure/_chat_client.py", {str(path) for path in core.files}
        )
        for name in ["agent-framework", "agent-framework-mem0", "mem0ai"]:
            with self.subTest(package=name), self.assertRaises(PackageNotFoundError):
                distribution(name)

    def test_test_imports_do_not_configure_azure_monitor(self):
        script = """
import os
import runpy
import sys
from unittest.mock import patch

with patch.dict(os.environ, {
    "APPLICATIONINSIGHTS_CONNECTION_STRING":
        "InstrumentationKey=00000000-0000-0000-0000-000000000000"
}), patch("azure.monitor.opentelemetry.configure_azure_monitor") as configure:
    runpy.run_path(sys.argv[1])
    configure.assert_not_called()
"""
        for name in ["test_app.py", "test_dependency_compatibility.py"]:
            with self.subTest(module=name):
                result = subprocess.run(
                    [sys.executable, "-c", script, str(Path(__file__).with_name(name))],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
