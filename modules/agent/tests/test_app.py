# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import app as api

PRIVATE_ERROR = "Backend failure at /private/service.py: secret-token"
PUBLIC_ERROR = "An unexpected error occurred. Please try again later."


@pytest.fixture
def client():
    # No lifespan context: these tests replace the agent without Azure services.
    return TestClient(api.app, raise_server_exceptions=False)


def assert_logged_exception(caplog, message):
    assert message in caplog.text
    assert any(
        record.exc_info and message in str(record.exc_info[1])
        for record in caplog.records
    )


def parse_events(response):
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.text.endswith("\n\n")
    return [
        json.loads(frame.removeprefix("data: "))
        for frame in response.text.strip().split("\n\n")
    ]


@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
@pytest.mark.parametrize("failure", ["exception", "error_event", "uninitialized"])
def test_chat_errors_are_sanitized(client, monkeypatch, caplog, endpoint, failure):
    async def run_stream(**kwargs):
        yield {"type": "text_delta", "content": "Partial response"}
        if failure == "exception":
            raise RuntimeError(PRIVATE_ERROR)
        yield {
            "type": "error",
            "content": PRIVATE_ERROR,
            "detail": PRIVATE_ERROR,
        }
        yield {"type": "done"}

    monkeypatch.setattr(
        api,
        "agent",
        None if failure == "uninitialized" else SimpleNamespace(run_stream=run_stream),
    )

    response = client.post(endpoint, json={"message": "Hello"})

    assert PRIVATE_ERROR not in response.text
    assert "Agent not initialized" not in response.text
    if endpoint == "/chat":
        assert response.status_code == 500
        assert response.json() == {
            "error": "Internal server error",
            "detail": PUBLIC_ERROR,
        }
    else:
        assert response.status_code == 200
        events = parse_events(response)
        assert len(events) == (1 if failure == "uninitialized" else 2)
        if failure != "uninitialized":
            assert events[0] == {
                "type": "text_delta",
                "content": "Partial response",
            }
        error = events[-1]
        assert set(error) == {"type", "content", "timestamp"}
        assert error["type"] == "error"
        assert error["content"] == PUBLIC_ERROR
        datetime.fromisoformat(error["timestamp"])

    assert_logged_exception(
        caplog, "Agent not initialized" if failure == "uninitialized" else PRIVATE_ERROR
    )


@pytest.mark.parametrize("endpoint", ["/chat", "/chat/stream"])
def test_successful_chat_responses_are_unchanged(client, monkeypatch, endpoint):
    events = [
        {"type": "text_delta", "content": "Hello "},
        {"type": "text_delta", "content": "world"},
        {"type": "done", "sources": [{"title": "Example"}]},
    ]
    calls = []

    async def run_stream(**kwargs):
        calls.append(kwargs)
        for event in events:
            yield event

    monkeypatch.setattr(api, "agent", SimpleNamespace(run_stream=run_stream))
    response = client.post(
        endpoint,
        json={
            "message": "Hello",
            "history": [{"user": "Earlier question", "assistant": "Earlier answer"}],
        },
    )

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["message"] == "Hello"
    assert [turn.model_dump() for turn in calls[0]["history"]] == [
        {"user": "Earlier question", "assistant": "Earlier answer"}
    ]
    if endpoint == "/chat":
        assert response.json() == {
            "response": "Hello world",
            "sources": [{"title": "Example"}],
        }
    else:
        assert parse_events(response) == events


def test_global_exception_handler_sanitizes_response(caplog):
    test_app = FastAPI()
    test_app.add_exception_handler(Exception, api.app.exception_handlers[Exception])

    @test_app.get("/unhandled")
    async def unhandled():
        raise RuntimeError(PRIVATE_ERROR)

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/unhandled")

    assert response.status_code == 500
    assert response.json() == {
        "error": "Internal server error",
        "detail": PUBLIC_ERROR,
    }
    assert PRIVATE_ERROR not in response.text
    assert_logged_exception(caplog, PRIVATE_ERROR)
