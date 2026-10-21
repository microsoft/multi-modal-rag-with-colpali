# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import base64
import os
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import torch
from colpali_engine.compression.token_pooling import HierarchicalTokenPooler
from PIL import Image
from transformers import AutoProcessor, Qwen2VLImageProcessorFast

from colpali_inference.inference import ColPaliInference
from colpali_inference.models import EmbedRequest


def image_request(pooling_types):
    with Image.new("RGB", (96, 96), "white") as image:
        stream = BytesIO()
        image.save(stream, format="PNG")
    return EmbedRequest(
        images=[base64.b64encode(stream.getvalue()).decode()],
        pooling_type=pooling_types,
    )


def image_inference():
    visual = torch.arange(1, 28, dtype=torch.float32).reshape(9, 3)
    raw = torch.cat([torch.full((4, 3), -1.0), visual, torch.full((3, 3), -2.0)])
    client = SimpleNamespace(
        tokenize_chat_image=AsyncMock(return_value=[1] * 4 + [99] * 9 + [2] * 3),
        embed_images=AsyncMock(return_value=[raw]),
    )
    inference = ColPaliInference(vllm_client=client)
    inference.is_initialized = True
    inference.processor = SimpleNamespace(
        image_token_id=99,
        image_token="<image>",
        visual_prompt_prefix="<start><image>Describe the image.",
        visual_prompt_suffix="<end>",
        tokenizer=SimpleNamespace(
            encode=Mock(return_value=[1] * 4 + [99] * 9 + [2] * 3)
        ),
        image_processor=SimpleNamespace(merge_size=2),
        get_n_patches=lambda *args, **kwargs: (3, 3),
    )
    inference.hierarchical_pooler = HierarchicalTokenPooler()
    inference._apply_hierarchical_pooling = Mock(
        wraps=inference._apply_hierarchical_pooling
    )
    return inference, client, visual


def text_inference():
    client = SimpleNamespace(embed_token_ids=AsyncMock())
    inference = ColPaliInference(vllm_client=client)
    inference.is_initialized = True
    inference.processor = SimpleNamespace(
        process_texts=Mock(
            return_value={
                "input_ids": torch.tensor([[1, 2], [3, 4]]),
                "attention_mask": torch.ones(2, 2),
            }
        )
    )
    return inference, client


class TokenSelectionTests(unittest.IsolatedAsyncioTestCase):
    def test_production_preprocessing_preserves_a4_and_bounds_oversized_images(self):
        inference, _, _ = image_inference()
        processor = Qwen2VLImageProcessorFast(
            patch_size=16, merge_size=2, min_pixels=65536, max_pixels=1310720
        )
        inference.processor.image_processor = processor
        with patch.dict(os.environ):
            os.environ.pop("COLPALI_MAX_IMAGE_PIXELS", None)
            inference._configure_image_cap()
            for size, expected_grid in (
                ((1240, 1754), [1, 110, 78]),
                ((2400, 2400), [1, 128, 128]),
            ):
                with self.subTest(size=size), Image.new("RGB", size) as image:
                    output = processor(images=image, return_tensors="pt")
                    self.assertEqual(
                        output["image_grid_thw"][0].tolist(), expected_grid
                    )
            os.environ["COLPALI_MAX_IMAGE_PIXELS"] = "1048576"
            inference._configure_image_cap()
            with Image.new("RGB", (2400, 2400)) as image:
                output = processor(images=image, return_tensors="pt")
                self.assertEqual(output["image_grid_thw"][0].tolist(), [1, 64, 64])
            for invalid in ("0", "-1", "true", "4.5"):
                os.environ["COLPALI_MAX_IMAGE_PIXELS"] = invalid
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    inference._configure_image_cap()

    async def test_every_image_output_retains_active_contextual_tokens(self):
        for modes in (
            ["none"],
            ["mean_pooling"],
            ["hierarchical_pooling"],
            ["none", "mean_pooling", "hierarchical_pooling"],
        ):
            with self.subTest(modes=modes):
                inference, client, visual = image_inference()
                response = await inference.generate_embeddings(image_request(modes))
                client.tokenize_chat_image.assert_awaited_once()
                raw = client.embed_images.return_value[0]
                if "none" in modes:
                    self.assertTrue(
                        torch.equal(torch.tensor(response.embeddings[0]), raw)
                    )
                if "mean_pooling" in modes:
                    rows = torch.tensor(response.mean_row_pooled_embeddings[0])
                    cols = torch.tensor(response.mean_column_pooled_embeddings[0])
                    self.assertTrue(
                        torch.equal(rows[4:-3], visual.reshape(3, 3, 3).mean(dim=1))
                    )
                    self.assertTrue(
                        torch.equal(cols[4:-3], visual.reshape(3, 3, 3).mean(dim=0))
                    )
                    for pooled in (rows, cols):
                        self.assertTrue(torch.equal(pooled[:4], raw[:4]))
                        self.assertTrue(torch.equal(pooled[-3:], raw[-3:]))
                if "hierarchical_pooling" in modes:
                    args = inference._apply_hierarchical_pooling.call_args.args
                    self.assertTrue(torch.equal(args[0].squeeze(0), raw))
                    self.assertEqual(
                        len(response.hierarchical_pooled_embeddings[0]), len(raw) // 3
                    )
                inference.processor.tokenizer.encode.assert_called_once_with(
                    "<start>" + "<image>" * 9 + "Describe the image.<end>",
                    add_special_tokens=False,
                )

    async def test_image_alignment_fails_closed_for_every_output(self):
        for mode in ("none", "mean_pooling", "hierarchical_pooling"):
            for failure in ("length", "grid", "empty_grid", "prompt", "batch"):
                with self.subTest(mode=mode, failure=failure):
                    inference, client, _ = image_inference()
                    if failure == "length":
                        client.embed_images.return_value = [torch.ones(15, 3)]
                    elif failure == "grid":
                        client.tokenize_chat_image.return_value = (
                            [1] * 5 + [99] * 8 + [2] * 3
                        )
                    elif failure == "empty_grid":
                        client.tokenize_chat_image.return_value = [1] * 16
                        inference.processor.get_n_patches = lambda *args, **kwargs: (
                            0,
                            0,
                        )
                    elif failure == "prompt":
                        client.tokenize_chat_image.return_value = (
                            [3] * 4 + [99] * 9 + [2] * 3
                        )
                    else:
                        client.embed_images.return_value = []
                    with self.assertRaisesRegex(RuntimeError, "mismatch|0 embeddings"):
                        await inference.generate_embeddings(image_request([mode]))

    async def test_mask_not_token_identity_controls_query_dispatch(self):
        for padding_side in ("left", "right"):
            with self.subTest(padding_side=padding_side):
                inference, client = text_inference()
                active = [151643, 5] + [151643] * 10
                other = [6, 7, 8, 9] + [151643] * 10
                padded = (
                    [151643] * 2 + active
                    if padding_side == "left"
                    else active + [151643] * 2
                )
                mask = (
                    [0] * 2 + [1] * 12 if padding_side == "left" else [1] * 12 + [0] * 2
                )
                inference.processor.process_texts.return_value = {
                    "input_ids": torch.tensor([padded, other]),
                    "attention_mask": torch.tensor([mask, [1] * 14]),
                }
                client.embed_token_ids.return_value = [
                    torch.ones(12, 3),
                    torch.ones(14, 3),
                ]
                response = await inference.generate_embeddings(
                    EmbedRequest(texts=["short", "longer query"])
                )
                inference.processor.process_texts.assert_called_once_with(
                    ["short", "longer query"]
                )
                client.embed_token_ids.assert_awaited_once_with(
                    [active, other], priority=None
                )
                self.assertEqual(
                    [len(matrix) for matrix in response.embeddings], [12, 14]
                )

    async def test_text_response_alignment_rejects_missing_or_padded_outputs(self):
        for output in ([], [torch.ones(3, 3), torch.ones(2, 3)]):
            with self.subTest(output=output):
                inference, client = text_inference()
                client.embed_token_ids.return_value = output
                with self.assertRaisesRegex(RuntimeError, "mismatch"):
                    await inference.generate_embeddings(EmbedRequest(texts=["a", "b"]))

    async def test_invalid_attention_masks_never_reach_vllm(self):
        for failure in ("shape", "value", "empty", "batch", "rank"):
            with self.subTest(failure=failure):
                inference, client = text_inference()
                batch = inference.processor.process_texts.return_value
                if failure == "shape":
                    batch["attention_mask"] = torch.ones(2, 3)
                elif failure == "value":
                    batch["attention_mask"][0, 0] = 2
                elif failure == "empty":
                    batch["attention_mask"][0] = 0
                elif failure == "batch":
                    batch["input_ids"] = torch.ones(1, 2, dtype=torch.long)
                    batch["attention_mask"] = torch.ones(1, 2)
                else:
                    batch["input_ids"] = torch.ones(2, dtype=torch.long)
                    batch["attention_mask"] = torch.ones(2)
                with self.assertRaisesRegex(RuntimeError, "mismatch|empty active"):
                    await inference.generate_embeddings(EmbedRequest(texts=["a", "b"]))
                client.embed_token_ids.assert_not_awaited()


@unittest.skipUnless(
    os.getenv("TEST_PROCESSOR_PATH"), "Requires a local checkpoint processor snapshot"
)
class CheckpointQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_checkpoint_keeps_query_augmentation_without_batch_padding(self):
        processor = AutoProcessor.from_pretrained(
            os.environ["TEST_PROCESSOR_PATH"],
            local_files_only=True,
            trust_remote_code=True,
        )
        queries = ["short", "a much longer retrieval query about document indexing"]
        expected = []
        for query in queries:
            batch = processor.process_texts([query])
            expected.append(
                batch["input_ids"][0][batch["attention_mask"][0].bool()].tolist()
            )
        self.assertNotEqual(len(expected[0]), len(expected[1]))

        async def embed_token_ids(token_ids, **kwargs):
            self.assertEqual(token_ids, expected)
            return [torch.ones(len(ids), 320) for ids in token_ids]

        inference = ColPaliInference(
            vllm_client=SimpleNamespace(embed_token_ids=embed_token_ids)
        )
        inference.processor = processor
        inference.is_initialized = True
        for padding_side in ("left", "right"):
            with self.subTest(padding_side=padding_side):
                processor.tokenizer.padding_side = padding_side
                response = await inference.generate_embeddings(EmbedRequest(texts=queries))
                self.assertEqual(
                    [len(rows) for rows in response.embeddings],
                    [len(ids) for ids in expected],
                )


class MeanPoolingTests(unittest.TestCase):
    def test_rectangular_grids_retain_context_outside_spatial_means(self):
        for x_patches, y_patches in ((4, 6), (7, 2), (1, 5), (5, 1)):
            with self.subTest(x=x_patches, y=y_patches):
                visual = torch.tensor(
                    [[y, x, 1] for y in range(y_patches) for x in range(x_patches)],
                    dtype=torch.float32,
                )
                embedding = torch.cat(
                    [torch.full((4, 3), -1.0), visual, torch.full((3, 3), -2.0)]
                )
                mask = torch.tensor([False] * 4 + [True] * len(visual) + [False] * 3)
                rows, cols = ColPaliInference.mean_pool_rows_and_columns(
                    embedding, mask, x_patches, y_patches
                )
                self.assertEqual(rows.shape, (4 + y_patches + 3, 3))
                self.assertEqual(cols.shape, (4 + x_patches + 3, 3))
                self.assertTrue(
                    torch.equal(rows[4:-3, 0], torch.arange(y_patches).float())
                )
                self.assertTrue(
                    torch.equal(cols[4:-3, 1], torch.arange(x_patches).float())
                )
                self.assertTrue(
                    torch.allclose(
                        rows[4:-3, 1], torch.full((y_patches,), (x_patches - 1) / 2)
                    )
                )
                self.assertTrue(
                    torch.allclose(
                        cols[4:-3, 0], torch.full((x_patches,), (y_patches - 1) / 2)
                    )
                )
                for pooled in (rows, cols):
                    self.assertTrue(torch.equal(pooled[:4], embedding[:4]))
                    self.assertTrue(torch.equal(pooled[-3:], embedding[-3:]))
                    self.assertTrue(torch.all(pooled[4:-3, 2] == 1))
