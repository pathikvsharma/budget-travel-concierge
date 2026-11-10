"""Verification tests for Pillars 2 & 4 (Persistent Sessions, Async Memory, Compaction, JSON Logs & Tracing)."""

from __future__ import annotations

import ast
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from google.adk.models.llm_request import LlmRequest
from google.genai import types
from pythonjsonlogger.json import JsonFormatter

from travel_concierge.database import fetch_preferences_async
from travel_concierge.memory import (
    build_events_compaction_config,
    compact_llm_request_history,
    create_persistent_session_service,
    flush_pending_memory_tasks,
    schedule_async_memory_consolidation,
)
from travel_concierge.observability import (
    ObservabilityPlugin,
    PiiRedactingLogFilter,
    RING_SPAN_EXPORTER,
    get_logger,
)


async def test_database_session_service_persists_state_across_reinstantiation(
    tmp_path: Path,
) -> None:
    db_file = tmp_path / "persisted_sessions.db"
    db_url = f"sqlite+aiosqlite:///{db_file.as_posix()}"

    service_1 = create_persistent_session_service(db_url=db_url)
    created = await service_1.create_session(
        app_name="travel_concierge",
        user_id="persistent_user_1",
        state={"preferred_destination": "LIS", "budget_usd": 1450},
    )
    assert db_file.exists()

    # Instantiate a brand-new DatabaseSessionService pointing at the same SQLite file.
    service_2 = create_persistent_session_service(db_url=db_url)
    reloaded = await service_2.get_session(
        app_name="travel_concierge",
        user_id="persistent_user_1",
        session_id=created.id,
    )
    assert reloaded is not None
    assert reloaded.state["preferred_destination"] == "LIS"
    assert reloaded.state["budget_usd"] == 1450


async def test_async_memory_consolidation_extracts_and_persists_without_blocking() -> None:
    task = schedule_async_memory_consolidation(
        traveler_id="async_user_42",
        user_text=(
            "I need vegan and gluten-free meals, an aisle seat, step-free access, "
            "and boutique hotels. My email is hidden@example.com."
        ),
    )
    # Returned immediately as a scheduled background asyncio.Task.
    assert not task.done() or task is not None
    await flush_pending_memory_tasks()

    saved = await fetch_preferences_async("async_user_42")
    by_cat = {row["category"]: row["preference_value"] for row in saved}
    assert "vegan" in by_cat["dietary"]
    assert "aisle seat" in by_cat["seat"]
    assert "step-free" in by_cat["accessibility"]
    assert "boutique hotel" in by_cat["accommodation"]
    for val in by_cat.values():
        assert "hidden@example.com" not in val


def test_history_compaction_summarizes_long_turn_windows() -> None:
    cfg = build_events_compaction_config()
    assert cfg.compaction_interval == 6
    assert cfg.overlap_size == 2

    many_turns = [
        types.Content(
            role="user" if i % 2 == 0 else "model",
            parts=[types.Part.from_text(text=f"Turn {i} discussing flights; email u{i}@test.org")],
        )
        for i in range(12)
    ]
    req = LlmRequest(model="gemini-2.5-flash", contents=many_turns)
    compacted = compact_llm_request_history(req, max_turns=5, agent_name="test_agent")
    assert compacted is True
    assert len(req.contents) == 5
    summary_text = req.contents[0].parts[0].text
    assert "[Compacted Prior Conversation Summary" in summary_text
    assert "@test.org" not in summary_text


async def test_observability_plugin_captures_structured_json_intent_outcome_and_otel_spans() -> None:
    stream = io.StringIO()
    logger = get_logger()
    capture_handler = logging.StreamHandler(stream)
    capture_handler.setFormatter(
        JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"levelname": "severity"},
        )
    )
    capture_handler.addFilter(PiiRedactingLogFilter())
    logger.addHandler(capture_handler)
    # Intent/outcome events log at INFO; don't depend on the ambient LOG_LEVEL.
    original_level = logger.level
    logger.setLevel(logging.INFO)

    try:
        plugin = ObservabilityPlugin()
        fake_tool = SimpleNamespace(name="search_scheduled_flights")
        fake_ctx = SimpleNamespace(
            agent_name="flight_research_agent",
            invocation_id="inv-test-123",
            function_call_id="fc-001",
            session=SimpleNamespace(id="sess-999"),
        )

        await plugin.before_tool_callback(
            tool=fake_tool,  # type: ignore[arg-type]
            tool_args={"origin": "NYC", "destination": "LIS", "note": "card 123-45-6789"},
            tool_context=fake_ctx,  # type: ignore[arg-type]
        )
        await plugin.after_tool_callback(
            tool=fake_tool,  # type: ignore[arg-type]
            tool_args={"origin": "NYC", "destination": "LIS"},
            tool_context=fake_ctx,  # type: ignore[arg-type]
            result={"status": "success", "matching_count": 2},
        )
    finally:
        logger.removeHandler(capture_handler)
        logger.setLevel(original_level)

    lines = [json.loads(line) for line in stream.getvalue().strip().splitlines() if line.strip()]
    event_types = [entry["event_type"] for entry in lines]
    assert "tool.intent" in event_types
    assert "tool.outcome" in event_types

    intent_log = next(e for e in lines if e["event_type"] == "tool.intent")
    outcome_log = next(e for e in lines if e["event_type"] == "tool.outcome")
    assert intent_log["correlation_id"] == outcome_log["correlation_id"]
    assert intent_log["trace_id"] != ""
    assert "123-45-6789" not in json.dumps(intent_log)
    assert outcome_log["intent_matched_outcome"] is True

    spans = RING_SPAN_EXPORTER.get_finished_spans()
    assert any(span.name == "tool.search_scheduled_flights" for span in spans)


def test_zero_bare_print_statements_in_agent_package() -> None:
    pkg_dir = Path(__file__).resolve().parent.parent / "travel_concierge"
    for py_file in pkg_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "print", f"Bare print call found in {py_file}"
