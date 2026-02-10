# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
"""Azure OpenAI agent for document Q&A with ColPali retrieval."""

import asyncio
import base64
import inspect
import json
import logging
import os
import re
from typing import List, Optional

from agent_framework import ChatMessage, DataContent, Role, TextContent
from agent_framework.azure import AzureOpenAIChatClient
from azure.identity import DefaultAzureCredential

from .document_retriever import DocumentRetriever
from .models import ChatTurn


class ColPaliAgent:
    def __init__(self, deployment_name: Optional[str] = None):
        """Initialize ColPali agent.

        Args:
            deployment_name: Optional Azure OpenAI deployment name. If not
                provided, defaults to environment configuration.
        """
        self.deployment_name = (
            deployment_name
            or os.getenv("AZURE_AI_MODEL_DEPLOYMENT_NAME")
            or os.getenv(
                "MODEL_NAME",
                "gpt-4o-mini",
            )
        )

        self.open_ai_endpoint = os.getenv("AI_FOUNDRY_OPEN_AI_ENDPOINT")

        if not self.open_ai_endpoint:
            raise ValueError(
                "Azure AI project endpoint is not configured. Set AI_FOUNDRY_OPEN_AI_ENDPOINT."
            )

        self.document_retriever = DocumentRetriever()

        self.debug_images_dir = os.path.join(os.path.dirname(__file__), "debug_images")
        os.makedirs(self.debug_images_dir, exist_ok=True)

        self.prompts = self._load_prompts()

        self._credential: Optional[DefaultAzureCredential] = None
        self._chat_client: Optional[AzureOpenAIChatClient] = None
        self._initialization_lock = asyncio.Lock()
        self._initialized = False

        prompts_cfg = self.prompts.get("prompts", {})
        self.query_agent_instructions = prompts_cfg.get("query_generation", "")
        self.response_agent_instructions = prompts_cfg.get("answer_generation", "")

        logging.info(
            "ColPali initialized for model deployment '%s' using Azure OpenAI Chat Client.",
            self.deployment_name,
        )

    def _load_prompts(self) -> dict:
        """Load prompts and configuration from YAML file.

        Returns:
            dict: Parsed prompts configuration.
        """
        import yaml

        prompt_file = os.path.join(os.path.dirname(__file__), "prompts.yaml")

        with open(prompt_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            logging.info("Loaded prompts configuration from %s", prompt_file)
            return data

    async def initialize(self) -> None:
        """Initialize Azure OpenAI chat client and dependencies.

        This method is idempotent and safe to call multiple times.
        """

        if self._initialized:
            return

        async with self._initialization_lock:
            if self._initialized:
                return

            credential = DefaultAzureCredential()
            try:
                chat_client = AzureOpenAIChatClient(
                    credential=credential,
                    endpoint=self.open_ai_endpoint,
                    deployment_name=self.deployment_name,
                )
            except Exception:
                await credential.close()
                raise

            self._credential = credential
            self._chat_client = chat_client
            self._initialized = True

    def _assert_initialized(self) -> None:
        if not self._initialized or not self._chat_client:
            raise RuntimeError("Agent not initialized. Call initialize() before use.")

    async def generate_search_queries(
        self,
        original_query: str,
        history: Optional[List[ChatTurn]] = None,
        num_queries: int = None,
    ) -> List[str]:
        """Generate multiple search queries from the original question.

        Args:
            original_query: User's original question.
            num_queries: Maximum number of additional queries to generate.
                If ``None``, all generated queries are used.

        Returns:
            list[str]: List containing the original query followed by
            generated variants.
        """
        try:
            logging.debug(
                "[Agent] Generating search queries. Original_query_len=%d, history_turns=%d",
                len(original_query),
                len(history) if history else 0,
            )

            system_prompt = self.query_agent_instructions

            # Incorporate chat history into the query generation prompt in a simple way.
            if history:
                history_lines = []
                for turn in history:
                    history_lines.append(f"User: {turn.user}")
                    if turn.assistant:
                        history_lines.append(f"Assistant: {turn.assistant}")
                history_text = "\n".join(history_lines)
                user_prompt = (
                    "Previous conversation (for context):\n"
                    + history_text
                    + "\n\nCurrent question: "
                    + original_query
                )
                logging.debug(
                    "[Agent] Built query-generation prompt with history. History_lines=%d",
                    len(history_lines),
                )
            else:
                user_prompt = original_query

            messages = [
                ChatMessage(
                    role=Role.SYSTEM, contents=[TextContent(text=system_prompt)]
                ),
                ChatMessage(role=Role.USER, contents=[TextContent(text=user_prompt)]),
            ]

            self._assert_initialized()
            chat_client = self._chat_client
            if not chat_client:
                raise RuntimeError("Chat client not initialized")
            response = await chat_client.get_response(messages)
            generated_text = response.text.strip()
            if not generated_text:
                raise RuntimeError("Query agent returned empty response")
            generated_queries = [
                q.strip() for q in generated_text.split("\n") if q.strip()
            ]

            # Include original query and limit to requested number
            all_queries = [original_query] + generated_queries[:num_queries]

            logging.debug(
                "Generated %d total queries: %s", len(all_queries), all_queries
            )
            return all_queries

        except Exception as e:
            logging.error("Failed to generate queries: %s", e)
            raise RuntimeError(f"Query generation failed: {e}") from e

    async def retrieve_documents_parallel(
        self, queries: List[str], top_k: int = None
    ) -> List:
        """Retrieve documents for multiple queries in parallel.

        Args:
            queries: Search queries to run against the index.
            top_k: Maximum number of unique document chunks to return. If
                ``None``, the retriever's default is used.

        Returns:
            list: Deduplicated list of document chunks.
        """
        try:
            # Use the simplified batch method that handles deduplication and limiting internally
            final_chunks = await self.document_retriever.search_documents_batch(
                queries, top_k
            )

            return final_chunks

        except Exception as e:
            logging.error("GPU-parallel retrieval failed: %s", e)
            raise RuntimeError(f"Document retrieval failed: {e}") from e

    async def run_stream(
        self,
        message: str,
        history: Optional[List[ChatTurn]] = None,
    ):
        """Run the agent with automatic retrieval and streaming output.

        Args:
            message: User message to answer.
            history: Optional ordered list of previous user/assistant turns.

        Yields:
            dict: Streaming events (text deltas, steps, sources, and
            completion markers) for the caller to render.
        """
        logging.debug(
            "[Agent] Starting run_stream. Message_len=%d, history_turns=%d",
            len(message),
            len(history) if history else 0,
        )
        try:
            self._assert_initialized()

            logging.info("Step 1: Generating search queries from user message")
            yield {
                "type": "step_start",
                "name": "Query Expansion",
                "type_name": "tool",
            }

            search_queries = await self.generate_search_queries(
                original_query=message,
                history=history,
            )

            query_list = "\n".join(
                [f"{i}. {query}" for i, query in enumerate(search_queries, 1)]
            )
            yield {
                "type": "step_end",
                "name": "Query Expansion",
                "output": f"Generated {len(search_queries)} search queries:\n{query_list}",
            }

            logging.info(
                "Step 2: Running parallel document retrieval for %d queries",
                len(search_queries),
            )
            yield {
                "type": "step_start",
                "name": "Document Search",
                "type_name": "tool",
            }

            document_chunks = await self.retrieve_documents_parallel(search_queries)
            original_chunk_count = len(document_chunks)
            logging.info(
                "Parallel document retrieval completed. Found %d unique chunks",
                original_chunk_count,
            )
            yield {
                "type": "step_end",
                "name": "Document Search",
                "output": f"Found {original_chunk_count} relevant document pages.",
            }

            # Send thinking step after document retrieval
            logging.info("Step 3: Analyzing retrieved documents")
            yield {
                "type": "step_start",
                "name": "Thinking",
                "type_name": "tool",
            }
            yield {
                "type": "step_end",
                "name": "Thinking",
                "output": "Analyzing retrieved documents and preparing response...",
            }

            for i, chunk in enumerate(document_chunks):
                logging.debug(
                    "Chunk %d: file=%s, page=%d, score=%.3f, has_image=%s",
                    i,
                    chunk.source_file,
                    chunk.page_number,
                    chunk.score,
                    bool(chunk.page_image_base64),
                )

            if not document_chunks:
                yield {
                    "type": "text_delta",
                    "content": "No relevant documents found. Please try a different query.\n",
                }
                yield {
                    "type": "done",
                    "total_searches": 1,
                }
                return

            logging.info(
                "Step 3: Creating multimodal input with %d document chunks",
                len(document_chunks),
            )
            contents = [TextContent(text=message)]

            for i, chunk in enumerate(document_chunks):
                citation_index = i + 1

                if chunk.page_image_base64:
                    # Save base64 image to local file for debugging
                    filename = f"{chunk.source_file.replace('.pdf', '')}_page_{chunk.page_number}.png"
                    # Clean filename for filesystem
                    filename = (
                        filename.replace("/", "_").replace("\\", "_").replace(":", "_")
                    )
                    image_path = os.path.join(self.debug_images_dir, filename)

                    try:
                        image_data = base64.b64decode(chunk.page_image_base64)
                        with open(image_path, "wb") as f:
                            f.write(image_data)
                    except Exception as save_error:
                        logging.warning(
                            "Failed to save debug image %s: %s", filename, save_error
                        )

                    # Add a textual JSON description of the chunk (excluding base64 image)
                    try:
                        chunk_dict = chunk.model_dump(exclude={"page_image_base64"})

                        text_prefix = (
                            f"Next document page (Citation {citation_index}):\n"
                        )
                        contents.append(
                            TextContent(
                                text=text_prefix
                                + json.dumps(chunk_dict, ensure_ascii=False, indent=2)
                            )
                        )
                    except Exception as json_error:
                        logging.warning(
                            "Failed to add JSON text description for chunk %s: %s",
                            chunk,
                            json_error,
                        )

                    contents.append(
                        DataContent(
                            uri=f"data:image/png;base64,{chunk.page_image_base64}",
                            media_type="image/png",
                        )
                    )
                elif chunk.text_content:
                    # Fallback to text content if image is not available
                    logging.info(
                        "Using text content fallback for citation %d: %s page %d",
                        citation_index,
                        chunk.source_file,
                        chunk.page_number,
                    )
                    try:
                        chunk_dict = chunk.model_dump()
                        text_prefix = f"Next document page (Citation {citation_index}) - Text Only:\n"
                        contents.append(
                            TextContent(
                                text=text_prefix
                                + json.dumps(chunk_dict, ensure_ascii=False, indent=2)
                            )
                        )
                    except Exception as json_error:
                        logging.warning(
                            "Failed to add text fallback for chunk %s: %s",
                            chunk,
                            json_error,
                        )

            logging.info(
                "Step 4: Creating ChatMessage with %d contents",
                len(contents),
            )
            user_chat_message = ChatMessage(
                role=Role.USER,
                contents=contents,
            )
            system_chat_message = ChatMessage(
                role=Role.SYSTEM,
                contents=[TextContent(text=self.response_agent_instructions)],
            )
            chat_messages = [system_chat_message, user_chat_message]
            logging.debug("ChatMessage payload prepared successfully")

            logging.info(
                "Step 5: Streaming response from Azure OpenAI deployment: %s",
                self.deployment_name,
            )

            self._assert_initialized()
            chat_client = self._chat_client
            if not chat_client:
                raise RuntimeError("Chat client not initialized")
            complete_response = ""

            try:
                stream = chat_client.get_streaming_response(chat_messages)
                async for chunk in stream:
                    text_delta = getattr(chunk, "text", None)
                    if text_delta is None and isinstance(chunk, dict):
                        text_delta = chunk.get("text") or chunk.get("delta") or ""

                    if text_delta:
                        yield {
                            "type": "text_delta",
                            "content": text_delta,
                        }
                        complete_response += text_delta
            except Exception as exc:
                logging.error(
                    "Azure OpenAI streaming interrupted: %s",
                    exc,
                )
                raise exc

            logging.info("Step 6: Processing response and replacing citations")

            citation_map = {}
            cited_sources = []
            seen_chunks = set()

            citation_pattern = r"\[(\d+)\]"
            cited_numbers = set(re.findall(citation_pattern, complete_response))
            for num_str in sorted(cited_numbers):  # Sort to maintain consistent order
                num = int(num_str)
                if 1 <= num <= len(document_chunks):
                    chunk = document_chunks[num - 1]  # Convert to 0-indexed
                    chunk_id = f"{chunk.source_file}_{chunk.page_number}"

                    # Replace [n] with the same numeric citation in text
                    citation_map[f"[{num}]"] = f"[{num}]"

                    # Add to sources for image display (avoid duplicates)
                    if chunk_id not in seen_chunks:
                        seen_chunks.add(chunk_id)
                        # Use the citation number directly so [1] -> source_1, [2] -> source_2, etc.
                        source_index = num
                        source_info = {
                            "title": f"{chunk.source_file} (page {chunk.page_number})",
                            "score": f"{chunk.score:.3f}",
                            "source": f"source_{source_index}",
                        }

                        # Add base64 image data if available
                        if chunk.page_image_base64:
                            source_info["base64_image"] = chunk.page_image_base64

                        cited_sources.append(source_info)

            # End the AI response generation step
            response_summary = f"Generated {len(complete_response)} character response"
            if citation_map:
                response_summary += f" with {len(citation_map)} citations processed"

            yield {
                "type": "step_end",
                "name": "AI Response Generation",
                "output": response_summary,
            }

            # If there are citations to replace, send the corrected version
            if citation_map:
                processed_response = complete_response
                for citation, replacement in citation_map.items():
                    processed_response = processed_response.replace(
                        citation, replacement
                    )

            # Emit sources as separate events (one per source) for UI rendering
            for source in cited_sources:
                yield {
                    "type": "source_delta",
                    "source": source,
                }

            yield {
                "type": "done",
            }

            logging.info("Agent run_stream completed successfully")

        except Exception as e:
            logging.error("Agent run_stream error: %s", e, exc_info=True)
            logging.error("Error occurred at message: %s", message)
            yield {
                "type": "error",
                "content": str(e),
            }

    async def cleanup(self):
        """Clean up resources.

        Closes credentials and Qdrant client (if present) and marks the
        agent as uninitialized.
        """
        if self._credential is not None:
            close_method = getattr(self._credential, "close", None)
            if callable(close_method):
                maybe_awaitable = close_method()
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable
            self._credential = None

        self._chat_client = None

        if hasattr(self.document_retriever, "qdrant_client"):
            qdrant_client = self.document_retriever.qdrant_client
            close_method = getattr(qdrant_client, "close", None)
            if callable(close_method):
                maybe_awaitable = close_method()
                if inspect.isawaitable(maybe_awaitable):
                    await maybe_awaitable

        self._initialized = False
        logging.info("Agent cleaned up")
