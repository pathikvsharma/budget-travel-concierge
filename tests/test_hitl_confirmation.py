"""Exercise ADK's real FunctionTool Human-in-the-Loop confirmation path.

These tests drive `FunctionTool.check_require_confirmation` and
`FunctionTool.run_async` exactly as the ADK runtime does (including the injected
`tool_context` keyword), catching signature mismatches that direct function
calls cannot reveal.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from travel_concierge.observability import classify_tool_outcome
from travel_concierge.tools import (
    cancel_confirmed_reservation_tool,
    reserve_approved_itinerary_tool,
)

_BOOKING_ARGS: dict[str, Any] = {
    "flight_id": "FL-LIS-201",
    "hotel_id": "HT-LIS-101",
    "departure_date": "2026-12-10",
    "nights": 4,
    "approved_budget_usd": 1100.0,
}


class _FakeToolContext(SimpleNamespace):
    """Minimal stand-in for the ToolContext attributes FunctionTool touches pre-execution."""

    def __init__(self, tool_confirmation: Any = None) -> None:
        super().__init__(
            tool_confirmation=tool_confirmation,
            function_call_id="fc-hitl-001",
            user_id="hitl_tester",
            state={},
            actions=SimpleNamespace(skip_summarization=False),
            confirmation_hints=[],
        )

    def request_confirmation(self, *, hint: str | None = None, payload: Any = None) -> None:
        self.confirmation_hints.append(hint)


async def test_adk_calls_booking_predicate_with_injected_tool_context() -> None:
    ctx = _FakeToolContext()
    high_value = await reserve_approved_itinerary_tool.check_require_confirmation(
        dict(_BOOKING_ARGS), ctx
    )
    low_value = await reserve_approved_itinerary_tool.check_require_confirmation(
        {**_BOOKING_ARGS, "approved_budget_usd": 120.0}, ctx
    )
    assert high_value is True
    assert low_value is False


async def test_high_value_booking_pauses_for_human_confirmation_before_execution() -> None:
    ctx = _FakeToolContext()
    result = await reserve_approved_itinerary_tool.run_async(
        args=dict(_BOOKING_ARGS), tool_context=ctx
    )
    assert "requires confirmation" in result["error"]
    assert ctx.confirmation_hints, "ADK must request explicit human confirmation"
    assert ctx.actions.skip_summarization is True
    assert classify_tool_outcome(result) == "awaiting_human_confirmation"


async def test_rejected_confirmation_never_executes_cancellation() -> None:
    ctx = _FakeToolContext(tool_confirmation=SimpleNamespace(confirmed=False))
    result = await cancel_confirmed_reservation_tool.run_async(
        args={"reservation_id": "TRV-ABCDEFGH", "reason": "Plans changed"},
        tool_context=ctx,
    )
    assert result == {"error": "This tool call is rejected."}
    assert classify_tool_outcome(result) == "rejected_by_human"
