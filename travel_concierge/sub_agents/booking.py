"""Booking specialist agent wrapping Human-in-the-Loop reservation & cancel tools."""

from __future__ import annotations

from google.adk.agents import LlmAgent

from travel_concierge.config import load_settings
from travel_concierge.prompts import BOOKING_AGENT_INSTRUCTION
from travel_concierge.tools.booking_tools import (
    cancel_confirmed_reservation_tool,
    lookup_reservation_status,
    reserve_approved_itinerary_tool,
)


def build_booking_execution_agent() -> LlmAgent:
    """Construct the HITL-protected reservation and cancellation specialist."""
    models = load_settings().models
    return LlmAgent(
        name="booking_execution_agent",
        model=models.select_model_for_task("booking_execution"),
        description=(
            "Executes approved flight+hotel reservations, cancels existing bookings "
            "(both protected by Human-in-the-Loop confirmation), and checks booking status."
        ),
        instruction=BOOKING_AGENT_INSTRUCTION,
        tools=[
            reserve_approved_itinerary_tool,
            cancel_confirmed_reservation_tool,
            lookup_reservation_status,
        ],
    )
