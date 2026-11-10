"""Parallel flight, hotel, and destination-advisory research specialists.

All three specialists execute concurrently inside `ParallelAgent` using the
low-latency `fast` model tier (default `gemini-3.5-flash`) and store grounded
outputs in distinct session state keys for downstream synthesis.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent, ParallelAgent

from travel_concierge.config import load_settings
from travel_concierge.prompts import (
    ADVISORY_RESEARCH_INSTRUCTION,
    FLIGHT_RESEARCH_INSTRUCTION,
    HOTEL_RESEARCH_INSTRUCTION,
)
from travel_concierge.tools.catalog_tools import (
    retrieve_destination_advisory,
    search_scheduled_flights,
    search_vetted_hotels,
)


def build_parallel_market_research() -> ParallelAgent:
    """Construct the 3-way `ParallelAgent` gathering travel options concurrently."""
    models = load_settings().models
    fast_tier = models.select_model_for_task("parallel_catalog_lookup")

    flight_research_agent = LlmAgent(
        name="flight_research_agent",
        model=fast_tier,
        description=(
            "Searches roundtrip flight schedules and group pricing across cabin tiers."
        ),
        instruction=FLIGHT_RESEARCH_INSTRUCTION,
        tools=[search_scheduled_flights],
        output_key="flight_research_findings",
    )

    hotel_research_agent = LlmAgent(
        name="hotel_research_agent",
        model=fast_tier,
        description=(
            "Searches vetted budget, boutique, and luxury hotels with stay subtotals."
        ),
        instruction=HOTEL_RESEARCH_INSTRUCTION,
        tools=[search_vetted_hotels],
        output_key="hotel_research_findings",
    )

    destination_advisory_agent = LlmAgent(
        name="destination_advisory_agent",
        model=fast_tier,
        description=(
            "Retrieves destination safety levels, visa rules, weather, and daily costs."
        ),
        instruction=ADVISORY_RESEARCH_INSTRUCTION,
        tools=[retrieve_destination_advisory],
        output_key="advisory_research_findings",
    )

    return ParallelAgent(
        name="parallel_market_research",
        description=(
            "Concurrently fetches flights, hotels, and destination advisories in parallel."
        ),
        sub_agents=[
            flight_research_agent,
            hotel_research_agent,
            destination_advisory_agent,
        ],
    )
