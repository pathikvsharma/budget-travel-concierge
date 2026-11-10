"""Sequential research-to-optimization pipeline with iterative budget `LoopAgent`.

Combines `ParallelAgent` market discovery with a bounded `LoopAgent` pairing the
`reasoning` tier (default `gemini-3.1-pro-preview`, multi-constraint itinerary
synthesis) and the `fast` tier (default `gemini-3.5-flash`, deterministic
compliance auditing and escalation).
"""

from __future__ import annotations

from google.adk.agents import LlmAgent, LoopAgent, SequentialAgent

from travel_concierge.config import load_settings
from travel_concierge.prompts import (
    BUDGET_CRITIC_INSTRUCTION,
    ITINERARY_ARCHITECT_INSTRUCTION,
)
from travel_concierge.sub_agents.research import build_parallel_market_research
from travel_concierge.tools.catalog_tools import (
    approve_budget_compliant_plan,
    calculate_itinerary_budget_breakdown,
)


def build_trip_planning_pipeline() -> SequentialAgent:
    """Construct the `SequentialAgent` orchestrating parallel lookup + budget loop."""
    models = load_settings().models

    itinerary_architect_agent = LlmAgent(
        name="itinerary_architect_agent",
        model=models.select_model_for_task("itinerary_synthesis"),
        description=(
            "Synthesizes parallel flight, hotel, advisory, and preference context "
            "into a day-by-day itinerary verified with exact budget math."
        ),
        instruction=ITINERARY_ARCHITECT_INSTRUCTION,
        tools=[calculate_itinerary_budget_breakdown],
        output_key="draft_itinerary_plan",
    )

    budget_compliance_critic_agent = LlmAgent(
        name="budget_compliance_critic_agent",
        model=models.select_model_for_task("compliance_audit"),
        description=(
            "Audits draft itineraries against budget ceilings, catalog IDs, and "
            "travel safety rules, escalating once compliant."
        ),
        instruction=BUDGET_CRITIC_INSTRUCTION,
        tools=[approve_budget_compliant_plan],
        output_key="budget_compliance_feedback",
    )

    budget_optimization_loop = LoopAgent(
        name="budget_optimization_loop",
        description=(
            "Iteratively drafts and refines the itinerary until budget and safety "
            "compliance checks pass (capped at 3 iterations)."
        ),
        sub_agents=[itinerary_architect_agent, budget_compliance_critic_agent],
        max_iterations=3,
    )

    return SequentialAgent(
        name="trip_planning_pipeline",
        description=(
            "End-to-end multi-day trip planning pipeline: runs parallel flight, "
            "hotel, and advisory discovery followed by iterative budget optimization. "
            "Delegate here when planning or comparing complete trips with a budget."
        ),
        sub_agents=[build_parallel_market_research(), budget_optimization_loop],
    )
