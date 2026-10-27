# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import base64
import json
import os
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import torch
from aiohttp import web
from aiohttp.test_utils import TestServer
from azure.monitor.opentelemetry.exporter import AzureMonitorLogExporter
from colpali_engine.compression.token_pooling import HierarchicalTokenPooler
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import SimpleLogRecordProcessor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import SpanKind
from PIL import Image
from transformers.generation.utils import GenerationMixin

from colpali_inference.app import app
from colpali_inference.download_model import download_model_from_hf
from colpali_inference.inference import ColPaliInference
from colpali_inference.models import EmbedRequest
from colpali_inference.vllm_client import (
    VLLMEmbeddingClient,
    VLLMUpstreamError,
)


class PoolingCompatibilityTests(unittest.TestCase):
    def test_custom_generation_refuses_download_without_trust(self):
        with (
            patch("transformers.generation.utils.has_file", return_value=True),
            patch("transformers.generation.utils.get_cached_module_file") as download,
        ):
            with self.assertRaises(ValueError):
                GenerationMixin().load_custom_generate(
                    "untrusted/model", trust_remote_code=False
                )
            download.assert_not_called()

    def test_hierarchical_pooling_uses_real_engine_on_cpu(self):
        handler = ColPaliInference()
        handler.hierarchical_pooler = HierarchicalTokenPooler()
        embeddings = torch.nn.functional.normalize(
            torch.arange(1, 97, dtype=torch.float32).reshape(1, 12, 8),
            dim=-1,
        )
        pooled = handler._apply_hierarchical_pooling(embeddings, {"pool_factor": 3})
        self.assertEqual(pooled.shape, (1, 4, 8))
        self.assertEqual(pooled.device.type, "cpu")
        self.assertTrue(torch.isfinite(pooled).all())

    def test_rectangular_mean_pooling_preserves_prefix_and_postfix(self):
        embeddings = torch.arange(16, dtype=torch.float32).reshape(8, 2)
        mask = torch.tensor([False, True, True, True, True, True, True, False])
        rows, columns = ColPaliInference.mean_pool_rows_and_columns(
            embeddings, mask, x_patches=3, y_patches=2
        )
        torch.testing.assert_close(rows, embeddings[[0, 2, 5, 7]])
        expected_columns = torch.cat(
            (embeddings[:1], (embeddings[1:4] + embeddings[4:7]) / 2, embeddings[-1:])
        )
        torch.testing.assert_close(columns, expected_columns)

    def test_pillow_decodes_rgba_data_uri_as_rgb(self):
        buffer = BytesIO()
        Image.new("RGBA", (28, 56), (10, 20, 30, 255)).save(buffer, format="PNG")
        encoded = (
            "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()
        )
        image = ColPaliInference().base64_to_image(encoded)
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(image.size, (28, 56))
        self.assertEqual(image.getpixel((0, 0)), (10, 20, 30))

    def test_local_processor_loading_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory, "model")
            model_path.mkdir()
            with (
                patch.dict(os.environ, {"MODEL_DIRECTORY_PATH": directory}),
                patch(
                    "colpali_inference.inference.AutoProcessor.from_pretrained"
                ) as load,
            ):
                handler = ColPaliInference(model_id="owner/model")
                handler.initialize()
                handler.initialize()
                load.assert_called_once_with(
                    str(model_path), local_files_only=True, trust_remote_code=True
                )
                self.assertTrue(handler.is_initialized)
                self.assertIsInstance(
                    handler.hierarchical_pooler, HierarchicalTokenPooler
                )

    @unittest.skipUnless(
        os.getenv("COLPALI_TEST_MODEL_DIRECTORY"),
        "Set COLPALI_TEST_MODEL_DIRECTORY to a downloaded processor's parent directory",
    )
    def test_real_offline_processor_and_patch_grid(self):
        with patch.dict(
            os.environ,
            {
                "MODEL_DIRECTORY_PATH": os.environ["COLPALI_TEST_MODEL_DIRECTORY"],
                "HF_HUB_OFFLINE": "1",
            },
        ):
            handler = ColPaliInference(
                model_id="TomoroAI/tomoro-ai-colqwen3-embed-4b-awq"
            )
            handler.initialize()
            self.assertTrue(handler.is_initialized)
            self.assertIsInstance(handler._get_image_token_id(), int)
            for size in ((224, 224), (448, 224), (224, 448)):
                with self.subTest(size=size):
                    x_patches, y_patches = handler._get_patches(size)
                    self.assertGreater(x_patches, 0)
                    self.assertGreater(y_patches, 0)
                    encoded = handler.processor.process_images([Image.new("RGB", size)])
                    image_tokens = encoded["input_ids"] == handler._get_image_token_id()
                    self.assertEqual(int(image_tokens.sum()), x_patches * y_patches)
                    if size[0] > size[1]:
                        self.assertGreater(x_patches, y_patches)
                    elif size[1] > size[0]:
                        self.assertGreater(y_patches, x_patches)


class DownloadCompatibilityTests(unittest.TestCase):
    def test_hub_snapshot_arguments_marker_and_vllm_config(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "model")

            def snapshot(**kwargs):
                self.assertEqual(
                    kwargs,
                    {
                        "repo_id": "owner/model",
                        "repo_type": "model",
                        "local_dir": str(target),
                    },
                )
                (target / "config.json").write_text(
                    json.dumps({"text_config": {"tie_word_embeddings": True}}),
                    encoding="utf-8",
                )

            with (
                patch.dict(
                    os.environ,
                    {"MODEL_DIRECTORY_PATH": directory, "MODEL_ID": "owner/model"},
                ),
                patch(
                    "colpali_inference.download_model.snapshot_download",
                    side_effect=snapshot,
                ) as download,
            ):
                self.assertEqual(download_model_from_hf(), str(target))
                self.assertEqual(download_model_from_hf(), str(target))
                download.assert_called_once()
            self.assertEqual((target / ".download_complete").read_text(), "owner/model")
            self.assertTrue(
                json.loads((target / "config.json").read_text())["tie_word_embeddings"]
            )


class HTTPCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_aiohttp_pooling_round_trip_and_upstream_error(self):
        requests = []

        async def pooling(request):
            body = await request.json()
            requests.append(body)
            if body["input"] == [[999]]:
                return web.Response(
                    status=429, text="busy", headers={"Retry-After": "2"}
                )
            return web.json_response({"data": [{"data": [[1.0, 0.0], [0.0, 1.0]]}]})

        server_app = web.Application()
        server_app.router.add_post("/pooling", pooling)
        async with TestServer(server_app) as server:
            client = VLLMEmbeddingClient(
                base_url=str(server.make_url("/")), model_id="test"
            )
            try:
                embeddings = await client.embed_token_ids([[101, 102]])
                torch.testing.assert_close(embeddings[0], torch.eye(2))
                self.assertEqual(
                    requests[0],
                    {
                        "model": "test",
                        "input": [[101, 102]],
                        "add_special_tokens": False,
                        "priority": 0,
                    },
                )
                with self.assertRaises(VLLMUpstreamError) as caught:
                    await client.embed_token_ids([[999]])
                self.assertEqual(caught.exception.status_code, 429)
                self.assertEqual(caught.exception.retry_after, "2")
            finally:
                await client.close()

    async def test_image_staging_cleanup_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            client = VLLMEmbeddingClient(image_shm_dir=directory)
            with self.assertRaisesRegex(RuntimeError, "upstream failed"):
                async with client._stage_images([Image.new("RGBA", (28, 28))]) as urls:
                    self.assertTrue(urls[0].startswith("file://"))
                    paths = list(Path(directory).glob("*.png"))
                    self.assertEqual(len(paths), 1)
                    with Image.open(paths[0]) as image:
                        self.assertEqual(image.mode, "RGB")
                    raise RuntimeError("upstream failed")
            self.assertEqual(list(Path(directory).iterdir()), [])

    @unittest.skipUnless(
        os.getenv("COLPALI_TEST_MODEL_DIRECTORY"),
        "Set COLPALI_TEST_MODEL_DIRECTORY to a downloaded processor's parent directory",
    )
    async def test_real_processor_query_and_image_shim_round_trip(self):
        requests = []
        image_ids = []

        async def handle(request):
            body = await request.json()
            requests.append(body)
            if "input" in body:
                self.assertIs(body["add_special_tokens"], False)
                sequences = body["input"]
            else:
                content = body["messages"][0]["content"]
                path = content[0]["image_url"]["url"].removeprefix("file://")
                with Image.open(path) as image:
                    sequences = handler.processor.process_images([image])[
                        "input_ids"
                    ].tolist()
                image_ids[:] = sequences[0]
                if request.path == "/tokenize":
                    return web.json_response({"tokens": sequences[0]})
            # Synthetic vectors exercise the shim, not GPU model numerical parity.
            return web.json_response(
                {
                    "data": [
                        {
                            "data": torch.nn.functional.normalize(
                                torch.arange(
                                    1, len(ids) * 4 + 1, dtype=torch.float32
                                ).reshape(len(ids), 4),
                                dim=-1,
                            ).tolist()
                        }
                        for ids in sequences
                    ]
                }
            )

        server_app = web.Application()
        server_app.router.add_post("/tokenize", handle)
        server_app.router.add_post("/pooling", handle)
        with tempfile.TemporaryDirectory() as directory:
            async with TestServer(server_app) as server:
                client = VLLMEmbeddingClient(
                    base_url=str(server.make_url("/")), image_shm_dir=directory
                )
                try:
                    handler = ColPaliInference(
                        model_id="TomoroAI/tomoro-ai-colqwen3-embed-4b-awq",
                        vllm_client=client,
                    )
                    with patch.dict(
                        os.environ,
                        {
                            "MODEL_DIRECTORY_PATH": os.environ[
                                "COLPALI_TEST_MODEL_DIRECTORY"
                            ],
                            "HF_HUB_OFFLINE": "1",
                        },
                    ):
                        handler.initialize()
                    queries = ["short", "a substantially longer retrieval query"]
                    batch = handler.processor.process_texts(queries)
                    expected_ids = [
                        ids[mask.bool()].tolist()
                        for ids, mask in zip(
                            batch["input_ids"], batch["attention_mask"]
                        )
                    ]
                    response = await handler.generate_embeddings(
                        EmbedRequest(texts=queries)
                    )
                    self.assertEqual(requests[0]["input"], expected_ids)
                    self.assertEqual(
                        [len(rows) for rows in response.embeddings],
                        [len(ids) for ids in expected_ids],
                    )
                    for ids in expected_ids:
                        self.assertEqual(
                            ids[-10:], [handler.processor.tokenizer.pad_token_id] * 10
                        )

                    stream = BytesIO()
                    with Image.new("RGB", (448, 224), "white") as image:
                        image.save(stream, format="PNG")
                    response = await handler.generate_embeddings(
                        EmbedRequest(
                            images=[base64.b64encode(stream.getvalue()).decode()],
                            pooling_type=[
                                "none",
                                "mean_pooling",
                                "hierarchical_pooling",
                            ],
                        )
                    )
                    x_patches, y_patches = handler._get_patches((448, 224))
                    context_tokens = len(image_ids) - x_patches * y_patches
                    self.assertEqual(len(response.embeddings[0]), len(image_ids))
                    self.assertEqual(
                        len(response.mean_row_pooled_embeddings[0]),
                        context_tokens + y_patches,
                    )
                    self.assertEqual(
                        len(response.mean_column_pooled_embeddings[0]),
                        context_tokens + x_patches,
                    )
                    self.assertEqual(
                        len(response.hierarchical_pooled_embeddings[0]),
                        len(image_ids) // 3,
                    )
                    self.assertEqual(list(Path(directory).iterdir()), [])
                finally:
                    await client.close()


class APICompatibilityTests(unittest.TestCase):
    def test_azure_monitor_exports_sdk_log_records(self):
        with patch.dict(
            os.environ,
            {
                "APPLICATIONINSIGHTS_CONTROLPLANE_DISABLED": "true",
                "APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL": "true",
                "APPLICATIONINSIGHTS_SDKSTATS_DISABLED": "true",
            },
        ):
            exporter = AzureMonitorLogExporter(
                connection_string=(
                    "InstrumentationKey=00000000-0000-0000-0000-000000000000"
                ),
                disable_offline_storage=True,
            )
            provider = LoggerProvider()
            provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
            try:
                with patch.object(
                    exporter.client,
                    "track",
                    return_value=SimpleNamespace(
                        errors=[], items_received=1, items_accepted=1
                    ),
                ) as send:
                    provider.get_logger(__name__).emit(
                        body="Embedding completed",
                        attributes={"document.id": "sample.pdf"},
                    )
                    send.assert_called_once()
                    envelopes = send.call_args.args[0]
                    self.assertEqual(len(envelopes), 1)
                    record = envelopes[0].data.base_data
                    self.assertEqual(record.message, "Embedding completed")
                    self.assertEqual(record.properties["document.id"], "sample.pdf")
            finally:
                provider.shutdown()
                exporter.client.close()

    def test_request_tracing_preserves_context_and_discards_oversized_tracestate(self):
        traced_app = FastAPI()

        @traced_app.get("/health")
        async def health():
            return {"status": "healthy"}

        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        FastAPIInstrumentor.instrument_app(traced_app, tracer_provider=provider)
        try:
            with TestClient(traced_app) as client:
                for tracestate, expected in (
                    ("vendor=value", [("vendor", "value")]),
                    ("vendor=value" + " " * 10000 + ",other=value", []),
                ):
                    with self.subTest(oversized=len(tracestate) > 8192):
                        exporter.clear()
                        response = client.get(
                            "/health",
                            headers={
                                "traceparent": (
                                    "00-0af7651916cd43dd8448eb211c80319c"
                                    "-b7ad6b7169203331-01"
                                ),
                                "tracestate": tracestate,
                            },
                        )
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json(), {"status": "healthy"})
                        spans = [
                            span
                            for span in exporter.get_finished_spans()
                            if span.kind == SpanKind.SERVER
                        ]
                        self.assertEqual(len(spans), 1)
                        span = spans[0]
                        self.assertEqual(
                            span.context.trace_id,
                            int("0af7651916cd43dd8448eb211c80319c", 16),
                        )
                        self.assertEqual(
                            span.parent.span_id, int("b7ad6b7169203331", 16)
                        )
                        self.assertEqual(
                            list(span.context.trace_state.items()), expected
                        )
        finally:
            FastAPIInstrumentor.uninstrument_app(traced_app)
            provider.shutdown()

    def test_liveness_and_request_validation_without_gpu_startup(self):
        client = TestClient(app)
        self.assertEqual(client.get("/livez").json(), {"status": "alive"})
        self.assertEqual(client.post("/embeddings", json={}).status_code, 400)
        self.assertEqual(
            client.post(
                "/embeddings", json={"texts": ["query"], "images": ["image"]}
            ).status_code,
            400,
        )
        self.assertEqual(
            client.post("/embeddings", json={"pooling_type": ["invalid"]}).status_code,
            422,
        )


if __name__ == "__main__":
    unittest.main()
