# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import json
from urllib.parse import parse_qs

from agent.document_retriever import DocumentRetriever
from azure.core.pipeline.transport import AioHttpTransport, AsyncHttpResponse
from qdrant_client import AsyncQdrantClient

from document_processor.document_processor import DocumentProcessor


async def test_indexer_and_image_retriever_exchange_the_projected_aks_token(
    monkeypatch, tmp_path
):
    token_file = tmp_path / "federated-token"
    token_file.write_text("synthetic-federated-assertion", encoding="utf-8")
    client_id = "11111111-1111-1111-1111-111111111111"
    tenant_id = "22222222-2222-2222-2222-222222222222"
    for name, value in {
        "AZURE_CLIENT_ID": client_id,
        "AZURE_TENANT_ID": tenant_id,
        "AZURE_FEDERATED_TOKEN_FILE": str(token_file),
        "AZURE_TOKEN_CREDENTIALS": "WorkloadIdentityCredential",
        "AZURE_AUTHORITY_HOST": "https://login.microsoftonline.com",
        "COLPALI_INFERENCE_ENDPOINT": "http://127.0.0.1:1",
        "QDRANT_ENDPOINT": "http://127.0.0.1:1",
    }.items():
        monkeypatch.setenv(name, value)
    exchanges = []

    class TokenResponse(AsyncHttpResponse):
        def __init__(self, request):
            super().__init__(request, None)
            self.status_code = 200
            self.headers = {"Content-Type": "application/json"}
            self.content_type = "application/json"

        def body(self):
            return json.dumps(
                {
                    "access_token": "local-test-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                }
            ).encode()

    async def exchange(_, request, **kwargs):
        assert request.url == (
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        ), "AKS must exchange its projected token, not call node managed identity"
        body = request.body
        if not isinstance(body, dict):
            body = {key: values[0] for key, values in parse_qs(body).items()}
        assert body["client_id"] == client_id
        assert body["client_assertion"] == "synthetic-federated-assertion"
        exchanges.append(body["scope"])
        return TokenResponse(request)

    monkeypatch.setattr(AioHttpTransport, "send", exchange)
    monkeypatch.delenv("QDRANT_ENDPOINT")
    monkeypatch.setattr(
        "agent.document_retriever.AsyncQdrantClient",
        lambda **_: AsyncQdrantClient(location=":memory:"),
    )
    processor = DocumentProcessor(require_service_bus=False)
    retriever = DocumentRetriever()
    try:
        for credential, scope in (
            (processor.credential, "https://storage.azure.com/.default"),
            (processor.credential, "https://servicebus.azure.net/.default"),
            (retriever.credential, "https://storage.azure.com/.default"),
        ):
            token = await credential.get_token(scope)
            assert token.token == "local-test-token"
        assert exchanges == [
            "https://storage.azure.com/.default",
            "https://servicebus.azure.net/.default",
            "https://storage.azure.com/.default",
        ]
    finally:
        await retriever.credential.close()
        await retriever.qdrant_client.close()
        await processor.close()
