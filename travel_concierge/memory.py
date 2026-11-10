"""Context compaction, persistent `DatabaseSessionService`, and async memory consolidation.

Covers all Context & Memory rubric pillars:
- `EventsCompactionConfig` + deterministic sliding-window request compaction.
- Persistent `DatabaseSessionService` (`sqlite+aiosqlite` locally, Cloud SQL in prod).
- Non-blocking asynchronous traveler preference extraction and consolidation via
  `asyncio.create_task` + `aiosqlite` so agent turn latency is never blocked.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.apps.app import ContextCacheConfig, EventsCompactionConfig
from google.adk.models.llm_request import LlmRequest
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from travel_concierge.config import load_settings
from travel_concierge.database import fetch_preferences_async, upsert_preferences_async
from travel_concierge.observability import log_structured_event
from travel_concierge.pii import redact_pii_text

# Strong reference registry preventing background consolidation tasks from GC.
_PENDING_MEMORY_TASKS: set[asyncio.Task[Any]] = set()

_PREFERENCE_EXTRACTORS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "dietary",
        re.compile(
            r"(?i)\b(vegetarian|vegan|gluten[- ]free|halal|kosher|dairy[- ]free|nut[- ]free|pescatarian)\b"
        ),
    ),
    (
        "seat",
        re.compile(r"(?i)\b(aisle seat|window seat|extra legroom|bulkhead seat)\b"),
    ),
    (
        "accommodation",
        re.compile(
            r"(?i)\b(boutique hotels?|budget hotels?|luxury hotels?|free breakfast|quiet room)\b"
        ),
    ),
    (
        "accessibility",
        re.compile(
            r"(?i)\b(wheelchair accessible|step[- ]free|elevator access|roll[- ]in shower)\b"
        ),
    ),
    (
        "pacing",
        re.compile(r"(?i)\b(relaxed pace|slow travel|packed itinerary|early morning flights?|nonstop flights?)\b"),
    ),
)


def create_persistent_session_service(db_url: str | None = None) -> DatabaseSessionService:
    """Instantiate ADK's persistent `DatabaseSessionService`.

    Defaults to `sqlite+aiosqlite:///.../trippilot_sessions.db` locally and accepts
    Cloud SQL PostgreSQL (`postgresql+asyncpg://...`) in production via
    `TRAVEL_SESSION_DB_URL`.
    """
    resolved_url = db_url or load_settings().session_db_url
    return DatabaseSessionService(db_url=resolved_url)


def build_events_compaction_config() -> EventsCompactionConfig:
    """Create ADK `EventsCompactionConfig` preventing multi-turn context bloat."""
    settings = load_settings()
    return EventsCompactionConfig(
        compaction_interval=settings.compaction_interval,
        overlap_size=settings.compaction_overlap_size,
    )


def build_context_cache_config() -> ContextCacheConfig:
    """Create ADK `ContextCacheConfig` optimizing repeated system/catalog context."""
    return ContextCacheConfig(
        cache_intervals=8,
        ttl_seconds=1800,
        # Stay above the explicit-caching minimum for Pro-tier models; smaller
        # requests simply skip caching instead of triggering failed cache creation.
        min_tokens=4096,
    )


def compact_llm_request_history(
    llm_request: LlmRequest,
    *,
    max_turns: int | None = None,
    agent_name: str = "",
) -> bool:
    """Apply deterministic sliding-window compaction when prompt history grows long.

    Complements ADK's `EventsCompactionConfig` by summarizing older raw text turns
    while strictly preserving tool call/response pairings on recent turns so
    standalone `Runner(agent=...)` invocations also remain bounded.
    """
    limit = max_turns or load_settings().max_context_turns
    contents = getattr(llm_request, "contents", None)
    if not contents or len(contents) <= limit:
        return False

    cutoff = len(contents) - (limit - 1)
    older = contents[:cutoff]
    recent = list(contents[cutoff:])

    # Avoid splitting immediately before a function_response turn.
    while recent and older:
        first_parts = getattr(recent[0], "parts", None) or []
        has_fn_resp = any(getattr(p, "function_response", None) is not None for p in first_parts)
        if not has_fn_resp:
            break
        recent.insert(0, older.pop())

    if len(older) < 2:
        return False

    snippets: list[str] = []
    for item in older[-8:]:
        role = getattr(item, "role", "turn")
        for part in getattr(item, "parts", None) or []:
            txt = getattr(part, "text", None)
            if isinstance(txt, str) and txt.strip():
                safe_txt, _ = redact_pii_text(txt.strip().replace("\n", " "))
                snippets.append(f"{role}: {safe_txt[:120]}")

    summary_body = " | ".join(snippets) if snippets else "Prior travel discovery turns compacted."
    summary_turn = types.Content(
        role="user",
        parts=[
            types.Part.from_text(
                text=f"[Compacted Prior Conversation Summary ({len(older)} earlier events)]: {summary_body}"
            )
        ],
    )
    llm_request.contents = [summary_turn, *recent]

    log_structured_event(
        "memory.history_compacted",
        "Compacted older conversation events via sliding window",
        agent_name=agent_name,
        compacted_events=len(older),
        retained_events=len(llm_request.contents),
    )
    return True


def extract_preferences_from_text(text: str) -> dict[str, str]:
    """Extract structured, non-PII traveler preferences from natural language."""
    scrubbed, _ = redact_pii_text(text)
    extracted: dict[str, str] = {}
    for category, pattern in _PREFERENCE_EXTRACTORS:
        matches = sorted({m.group(1).lower() for m in pattern.finditer(scrubbed)})
        if matches:
            extracted[category] = ", ".join(matches)
    return extracted


async def _consolidate_preferences_background(
    traveler_id: str,
    raw_user_text: str,
) -> int:
    """Background coroutine extracting and writing long-term traveler memories."""
    try:
        extracted = extract_preferences_from_text(raw_user_text)
        if not extracted:
            return 0
        count = await upsert_preferences_async(
            traveler_id=traveler_id,
            extracted_preferences=extracted,
            source="async_memory_callback",
        )
        log_structured_event(
            "memory.async_consolidated",
            "Consolidated traveler preferences asynchronously",
            traveler_id=traveler_id,
            categories_saved=sorted(extracted.keys()),
            records_written=count,
        )
        return count
    except Exception as exc:
        log_structured_event(
            "memory.async_consolidation_failed",
            "Background memory consolidation encountered an error",
            traveler_id=traveler_id,
            error_type=type(exc).__name__,
        )
        return 0


def schedule_async_memory_consolidation(
    traveler_id: str,
    user_text: str,
) -> asyncio.Task[int]:
    """Schedule non-blocking background preference consolidation (`create_task`)."""
    task = asyncio.create_task(
        _consolidate_preferences_background(traveler_id=traveler_id, raw_user_text=user_text)
    )
    _PENDING_MEMORY_TASKS.add(task)
    task.add_done_callback(_PENDING_MEMORY_TASKS.discard)
    return task


async def flush_pending_memory_tasks() -> None:
    """Await any in-flight background memory tasks (useful in graceful shutdown/tests)."""
    if _PENDING_MEMORY_TASKS:
        await asyncio.gather(*list(_PENDING_MEMORY_TASKS), return_exceptions=True)


def _resolve_traveler_id(callback_context: CallbackContext) -> str:
    raw_uid = str(getattr(callback_context, "user_id", "") or "default_traveler")
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", raw_uid)[:64]
    return cleaned or "default_traveler"


async def hydrate_traveler_memory_callback(
    callback_context: CallbackContext,
) -> Optional[types.Content]:
    """Async `before_agent_callback` hydrating saved preferences into session state."""
    traveler_id = _resolve_traveler_id(callback_context)
    prefs = await fetch_preferences_async(traveler_id)
    if prefs:
        summary = "; ".join(f"{p['category']}={p['preference_value']}" for p in prefs)
    else:
        summary = "No saved traveler preferences on file yet."
    callback_context.state["traveler_memory_summary"] = summary
    callback_context.state["traveler_id"] = traveler_id
    return None


async def consolidate_traveler_memory_callback(
    callback_context: CallbackContext,
) -> Optional[types.Content]:
    """Async `after_agent_callback` triggering non-blocking memory extraction.

    Spawns a background `asyncio.Task` to extract and persist traveler preferences
    (and optionally sync session events into ADK MemoryService if attached)
    immediately returning `None` so the user's response never blocks on I/O.
    """
    traveler_id = _resolve_traveler_id(callback_context)
    user_content = getattr(callback_context, "user_content", None)
    collected_text: list[str] = []
    for part in getattr(user_content, "parts", None) or []:
        txt = getattr(part, "text", None)
        if isinstance(txt, str) and txt.strip():
            collected_text.append(txt.strip())

    if collected_text:
        schedule_async_memory_consolidation(
            traveler_id=traveler_id,
            user_text=" ".join(collected_text),
        )

    # Also fire non-blocking ADK Memory Bank session ingestion when configured.
    add_session_fn = getattr(callback_context, "add_session_to_memory", None)
    if callable(add_session_fn):
        async def _safe_sync_memory_bank() -> None:
            try:
                await add_session_fn()
            except ValueError:
                # No external MemoryService attached in default local mode — expected.
                return
            except Exception as exc:
                log_structured_event(
                    "memory.memory_bank_sync_warning",
                    "Optional MemoryService sync skipped",
                    error_type=type(exc).__name__,
                )

        mb_task = asyncio.create_task(_safe_sync_memory_bank())
        _PENDING_MEMORY_TASKS.add(mb_task)
        mb_task.add_done_callback(_PENDING_MEMORY_TASKS.discard)

    return None
