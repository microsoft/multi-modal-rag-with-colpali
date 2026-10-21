"""Bounded, database-free embedding throughput measurement against a live shim."""

import argparse
import asyncio
import base64
import io
import json
import random
import statistics
import sys
import time

import aiohttp
import numpy as np
from PIL import Image, ImageDraw

IMAGE_FIELDS = (
    "hierarchical_pooled_embeddings",
    "mean_row_pooled_embeddings",
    "mean_column_pooled_embeddings",
)


def make_page(number: int) -> str:
    """Synthetic A4 page at 150 DPI; vary content to avoid identical requests."""
    with Image.new("RGB", (1240, 1754), "white") as image:
        draw = ImageDraw.Draw(image)
        draw.text((60, 50), f"Annual financial report - page {number}", fill="black")
        for row in range(45):
            y = 110 + row * 32
            draw.text(
                (60, y),
                f"Division {row + 1}: revenue {number * 137 + row * 53} USD. "
                "Operating results and annual projections.",
                fill="black",
            )
            if row % 5 == 0:
                draw.rectangle(
                    (800, y, 800 + (row + number) % 300, y + 20), fill="navy"
                )
        stream = io.BytesIO()
        image.save(stream, format="PNG")
    return base64.b64encode(stream.getvalue()).decode()


def validate(body: dict, fields: tuple[str, ...], count: int) -> None:
    for field in fields:
        batches = body.get(field)
        if not isinstance(batches, list) or len(batches) != count:
            raise ValueError(f"{field}: expected {count} pages")
        for values in batches:
            matrix = np.asarray(values)
            if (
                matrix.ndim != 2
                or matrix.shape[0] == 0
                or matrix.shape[1] != 320
                or not np.isfinite(matrix).all()
                or not np.any(matrix != 0, axis=1).all()
            ):
                raise ValueError(f"{field}: invalid embedding vectors")


async def request(session, url, payload, fields, count):
    started = time.perf_counter()
    async with session.post(url + "/embeddings", json=payload) as response:
        if response.status >= 400:
            detail = (await response.text())[:2048]
            raise RuntimeError(f"Embedding HTTP {response.status}: {detail}")
        body = await response.json()
    validate(body, fields, count)
    return time.perf_counter() - started


def latency_summary(values):
    if not values:
        return None
    return {
        "count": len(values),
        "median_seconds": statistics.median(values),
        "p95_seconds": float(np.percentile(values, 95)),
        "max_seconds": max(values),
    }


async def measure(session, args, images, batch_size, concurrency):
    queue = asyncio.Queue()
    for offset in range(0, len(images), batch_size):
        queue.put_nowait(images[offset : offset + batch_size])
    latencies = []
    query_latencies = []
    completed_pages = 0
    finished = asyncio.Event()
    gpu_samples = []

    async def worker():
        nonlocal completed_pages
        while not queue.empty():
            batch = queue.get_nowait()
            elapsed = await request(
                session,
                args.url,
                {
                    "images": batch,
                    "pooling_type": ["hierarchical_pooling", "mean_pooling"],
                    "pooling_config": {"pool_factor": 3},
                },
                IMAGE_FIELDS,
                len(batch),
            )
            latencies.append(elapsed)
            completed_pages += len(batch)

    async def queries():
        while not finished.is_set():
            elapsed = await request(
                session,
                args.url,
                {"texts": ["What is the annual revenue?"], "pooling_type": ["none"]},
                ("embeddings",),
                1,
            )
            query_latencies.append(elapsed)
            try:
                await asyncio.wait_for(finished.wait(), timeout=args.query_interval)
            except TimeoutError:
                pass

    async def sample_gpu():
        while not finished.is_set():
            process = await asyncio.create_subprocess_exec(
                "nvidia-smi",
                "--query-gpu=memory.used,utilization.gpu",
                "--format=csv,noheader,nounits",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await process.communicate()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            if process.returncode:
                raise RuntimeError(stderr.decode())
            gpu_samples.append(
                [int(value.strip()) for value in stdout.decode().split(",")]
            )
            try:
                await asyncio.wait_for(finished.wait(), timeout=1)
            except TimeoutError:
                pass

    started = time.perf_counter()
    async with asyncio.TaskGroup() as tasks:
        if args.sample_gpu:
            tasks.create_task(sample_gpu())
        if args.query_interval:
            tasks.create_task(queries())
        workers = [tasks.create_task(worker()) for _ in range(concurrency)]
        await asyncio.gather(*workers)
        seconds = time.perf_counter() - started
        finished.set()
    if completed_pages != len(images):
        raise RuntimeError(f"Completed {completed_pages}/{len(images)} pages")
    return {
        "batch_size": batch_size,
        "concurrent_requests": concurrency,
        "max_inflight_pages": batch_size * concurrency,
        "pages": completed_pages,
        "seconds": seconds,
        "pages_per_second": completed_pages / seconds,
        "image_requests": latency_summary(latencies),
        "queries": latency_summary(query_latencies),
        "gpu": {
            "samples": len(gpu_samples),
            "peak_memory_mib": max(sample[0] for sample in gpu_samples),
            "mean_utilization_percent": statistics.mean(
                sample[1] for sample in gpu_samples
            ),
        }
        if gpu_samples
        else None,
    }


async def main(args):
    configurations = []
    for value in args.configurations.split(","):
        batch, concurrency = map(int, value.split(":"))
        if min(batch, concurrency) < 1:
            raise ValueError("Batch size and concurrency must be positive")
        configurations.append((batch, concurrency))
    if args.pages < 1 or args.repeats < 1 or args.query_interval < 0:
        raise ValueError(
            "Pages/repeats must be positive and query interval nonnegative"
        )
    images = [make_page(number) for number in range(args.pages)]
    timeout = aiohttp.ClientTimeout(total=120)
    # Match the indexer's fresh connection for each embedding request.
    connector = aiohttp.TCPConnector(force_close=True)
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        async with session.get(args.url + "/health") as response:
            response.raise_for_status()
            health = await response.json()
        print(
            json.dumps(
                {"event": "configuration", "arguments": vars(args), "health": health}
            ),
            flush=True,
        )
        await request(
            session,
            args.url,
            {
                "images": images[:1],
                "pooling_type": ["hierarchical_pooling", "mean_pooling"],
            },
            IMAGE_FIELDS,
            1,
        )
        results = []
        rng = random.Random(42)
        for repeat in range(args.repeats):
            order = sorted(
                configurations, key=lambda pair: (pair[0] * pair[1], pair[0])
            )
            if repeat:
                rng.shuffle(order)
            for batch, concurrency in order:
                print(
                    json.dumps(
                        {
                            "event": "measurement_start",
                            "batch_size": batch,
                            "concurrent_requests": concurrency,
                            "repeat": repeat + 1,
                        }
                    ),
                    flush=True,
                )
                result = await measure(session, args, images, batch, concurrency)
                result["repeat"] = repeat + 1
                results.append(result)
                print(json.dumps({"event": "measurement", **result}), flush=True)
        print(json.dumps({"event": "complete", "results": results}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--configurations", default="1:1,1:2,1:4,1:8,1:10,1:16,2:4,4:2")
    parser.add_argument("--pages", type=int, default=24)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--query-interval", type=float, default=0)
    parser.add_argument(
        "--sample-gpu",
        action="store_true",
        help="Sample a single local GPU with nvidia-smi",
    )
    arguments = parser.parse_args()
    arguments.url = arguments.url.rstrip("/")
    try:
        asyncio.run(main(arguments))
    except Exception as error:
        print(
            json.dumps({"event": "error", "error": str(error)}),
            file=sys.stderr,
            flush=True,
        )
        raise
