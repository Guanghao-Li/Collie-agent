from __future__ import annotations

from pathlib import Path
from typing import Any
import json
import logging

from agent.llm import LLMProvider
from bootstrap.config import MemoryConfig
from memory.consolidator import MemoryConsolidator
from memory.default_engine import DefaultMemoryEngine
from memory.engine import MemoryEngine, MemoryMutation, MemoryQuery
from memory.extractor import MemoryExtractor
from memory.models import (
    ConsolidationResult,
    MemoryGateDecision,
    MemoryItem,
    MemorySearchResult,
    MemorySearchTrace,
)
from memory.search import MemorySearch
from memory.store import MemoryStore
from session.models import SessionMessage


class MemoryRuntime:
    def __init__(
        self,
        workspace: str | Path,
        config: MemoryConfig,
        llm_provider: LLMProvider | None = None,
        fast_llm_provider: LLMProvider | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.config = config
        self.main_llm_provider = llm_provider
        self.fast_llm_provider = fast_llm_provider or llm_provider
        self.store = MemoryStore(self.workspace / config.workspace_dir)
        self.search_engine = MemorySearch()
        self.engine: MemoryEngine = DefaultMemoryEngine(
            self.workspace / config.workspace_dir,
            config=config,
            store=self.store,
            search_engine=self.search_engine,
        )
        self.extractor = MemoryExtractor(
            main_llm_provider=llm_provider,
            fast_llm_provider=self.fast_llm_provider,
        )
        self.consolidator = MemoryConsolidator(
            self.store,
            main_llm_provider=llm_provider,
            fast_llm_provider=self.fast_llm_provider,
            markdown_store=self.engine.markdown_store,  # type: ignore[attr-defined]
            config=config,
        )
        self.last_search_trace: MemorySearchTrace | None = None
        self._logger = logging.getLogger(__name__)

    async def initialize(self) -> None:
        await self.engine.initialize()

    async def read_core_memory(self) -> str:
        result = await self.engine.query(MemoryQuery(kind="core"))
        return result.content

    async def read_profile(self) -> str:
        result = await self.engine.query(MemoryQuery(kind="profile"))
        return result.content

    async def read_recent_context(self) -> str:
        result = await self.engine.query(MemoryQuery(kind="recent_context"))
        return result.content

    async def append_pending_memory(self, item: MemoryItem) -> None:
        await self.engine.mutate(MemoryMutation(kind="remember", item=item, stable=False))

    async def add_memory(self, item: MemoryItem) -> None:
        await self.engine.mutate(MemoryMutation(kind="remember", item=item, stable=True))

    async def delete_memory(self, memory_id: str, reason: str) -> None:
        await self.engine.mutate(
            MemoryMutation(kind="forget", memory_id=memory_id, reason=reason)
        )

    async def search(self, query: str, limit: int = 8) -> list[MemoryItem]:
        result = await self.engine.query(MemoryQuery(kind="search", text=query, limit=limit))
        return result.items

    async def should_search_memory(
        self,
        user_message: str,
        recent_messages: list[SessionMessage] | None = None,
    ) -> MemoryGateDecision:
        provider = self.fast_llm_provider
        if provider is None or getattr(provider, "name", "") == "echo":
            return self._rule_based_gate(user_message)

        prompt = (
            "Decide whether we should search long-term memory for this user message. "
            "Return JSON only with keys should_search, reason, query_type, suggested_query, "
            "memory_types.\n\n"
            f"User message: {user_message}\n"
            f"Recent messages:\n{self._render_recent_messages(recent_messages or [])}"
        )
        try:
            response = await provider.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                timeout_seconds=10,
                purpose="memory_gate",
            )
            data: dict[str, Any] = json.loads(response)
            return MemoryGateDecision(
                should_search=bool(data.get("should_search", True)),
                reason=str(data.get("reason", "fast_model")),
                query_type=str(data.get("query_type", "broad")),
                suggested_query=(
                    str(data["suggested_query"]) if data.get("suggested_query") else None
                ),
                memory_types=[str(item) for item in data.get("memory_types", [])],
            )
        except Exception:
            self._logger.warning(
                "memory gate parsing failed; defaulting to search",
                exc_info=True,
            )
            return MemoryGateDecision(
                should_search=True,
                reason="fallback_on_parse_error",
                query_type="broad",
                suggested_query=user_message,
                memory_types=[],
            )

    async def rewrite_memory_query(
        self,
        user_message: str,
        recent_messages: list[SessionMessage] | None = None,
        gate: MemoryGateDecision | None = None,
    ) -> str:
        if gate and gate.suggested_query:
            return gate.suggested_query
        provider = self.fast_llm_provider
        if provider is None or getattr(provider, "name", "") == "echo":
            recent = " ".join(message.content for message in (recent_messages or [])[-2:])
            return f"{recent} {user_message}".strip() or user_message

        prompt = (
            "Rewrite the user request into a single memory-search query. "
            "Return the rewritten query only.\n\n"
            f"User message: {user_message}\n"
            f"Recent messages:\n{self._render_recent_messages(recent_messages or [])}"
        )
        try:
            response = await provider.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                timeout_seconds=10,
                purpose="memory_query_rewrite",
            )
            return response.strip().splitlines()[0] or user_message
        except Exception:
            self._logger.warning(
                "memory query rewrite failed; using the original message",
                exc_info=True,
            )
            return user_message

    async def generate_hyde_document(
        self,
        query: str,
        recent_messages: list[SessionMessage] | None = None,
    ) -> str | None:
        if not self.config.enable_hyde:
            return None
        provider = self.fast_llm_provider
        if provider is None or getattr(provider, "name", "") == "echo":
            return None

        prompt = (
            "Write a short hypothetical memory snippet that would help retrieve relevant "
            "long-term memories for this query. Return plain text only.\n\n"
            f"Query: {query}\n"
            f"Recent messages:\n{self._render_recent_messages(recent_messages or [])}"
        )
        try:
            response = await provider.complete(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                timeout_seconds=10,
                purpose="memory_hyde",
            )
            return response.strip() or None
        except Exception:
            self._logger.warning("HyDE generation failed; continuing without it", exc_info=True)
            return None

    async def search_with_trace(
        self,
        user_message: str,
        recent_messages: list[SessionMessage] | None = None,
        limit: int | None = None,
    ) -> MemorySearchResult:
        gate = await self.should_search_memory(user_message, recent_messages or [])
        rewritten_query: str | None = None
        hyde_document: str | None = None
        memories: list[MemoryItem] = []

        if gate.should_search:
            rewritten_query = await self.rewrite_memory_query(
                user_message,
                recent_messages or [],
                gate,
            )
            hyde_document = await self.generate_hyde_document(
                rewritten_query,
                recent_messages or [],
            )
            combined_query = (
                f"{rewritten_query}\n{hyde_document}" if hyde_document else rewritten_query
            )
            memories = await self.search(combined_query, limit or self.config.search_limit)

        trace = MemorySearchTrace(
            gate_decision=gate,
            original_query=user_message,
            rewritten_query=rewritten_query,
            hyde_document=hyde_document,
            used_fast_model=self.fast_llm_provider is not None,
            fast_model_name=getattr(self.fast_llm_provider, "model", None)
            or getattr(self.fast_llm_provider, "name", None),
            selected_memory_ids=[item.id for item in memories],
        )
        self.last_search_trace = trace
        return MemorySearchResult(memories=memories, trace=trace)

    async def extract_from_turn(
        self,
        session_id: str,
        user_message: str,
        assistant_message: str,
    ) -> list[MemoryItem]:
        if not self.config.enabled or not self.config.auto_extract:
            return []
        recent_context = await self.read_recent_context()
        items = await self.extractor.extract(
            session_id=session_id,
            user_message=user_message,
            assistant_message=assistant_message,
            recent_context=recent_context,
        )
        if self.config.consolidation_mode != "aka_like":
            items = [
                item
                for item in items
                if item.metadata.get("extraction_kind") != "history_entry"
                and "history_entry" not in item.tags
            ]
        for item in items:
            await self.append_pending_memory(item)
        return items

    async def consolidate(self) -> ConsolidationResult:
        result = await self.consolidator.consolidate()
        if self.config.consolidation_mode != "aka_like":
            await self.engine.mutate(MemoryMutation(kind="sync"))
        return result

    async def update_recent_context(self, summary: str) -> None:
        await self.engine.mutate(MemoryMutation(kind="replace_recent_context", content=summary))

    async def append_reflection(self, summary: str) -> None:
        await self.engine.mutate(MemoryMutation(kind="append_history", content=summary))

    async def build_memory_context(
        self,
        query: str,
        recent_messages: list[SessionMessage] | None = None,
    ) -> str:
        profile = await self.read_profile()
        recent_context = await self.read_recent_context()
        search_result = await self.search_with_trace(
            query,
            recent_messages or [],
            self.config.search_limit,
        )
        results = search_result.memories
        lines = [profile.strip(), recent_context.strip()]
        if results:
            lines.append("Relevant memories:")
            lines.extend(f"- {item.text}" for item in results)
        return "\n\n".join(line for line in lines if line)

    async def stats(self) -> dict[str, int]:
        result = await self.engine.query(MemoryQuery(kind="stats"))
        stats = result.metadata.get("stats", {})
        return {
            "active": int(stats.get("active", 0)),
            "pending": int(stats.get("pending", 0)),
            "deleted": int(stats.get("deleted", 0)),
        }

    def _rule_based_gate(self, user_message: str) -> MemoryGateDecision:
        normalized = user_message.strip().lower()
        short_no_search = {
            "好",
            "好的",
            "谢谢",
            "继续",
            "ok",
            "okay",
            "thanks",
            "thank you",
            "yes",
            "no",
        }
        if normalized in short_no_search or len(normalized) <= 2:
            return MemoryGateDecision(
                should_search=False,
                reason="short_acknowledgement",
                query_type="none",
            )

        search_keywords = [
            "之前",
            "记得",
            "我说过",
            "上次",
            "偏好",
            "喜欢",
            "项目",
            "目标",
            "记忆",
            "remember",
            "before",
            "preference",
            "project",
            "goal",
        ]
        if any(keyword in normalized for keyword in search_keywords):
            return MemoryGateDecision(
                should_search=True,
                reason="keyword_hit",
                query_type="broad",
                suggested_query=user_message,
            )
        return MemoryGateDecision(
            should_search=True,
            reason="default_search",
            query_type="broad",
            suggested_query=user_message,
        )

    def _render_recent_messages(self, recent_messages: list[SessionMessage]) -> str:
        if not recent_messages:
            return "(none)"
        return "\n".join(
            f"{message.role}: {message.content}" for message in recent_messages[-6:]
        )
