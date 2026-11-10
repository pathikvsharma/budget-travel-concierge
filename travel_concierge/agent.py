"""Root ADK `App`, coordinator `LlmAgent`, and persistent `Runner` entrypoint.

Exposes:
- `root_agent`: Hierarchical TripPilot coordinator combining direct lookup/memory
  tools with `trip_planning_pipeline` (`SequentialAgent` -> `ParallelAgent` +
  `LoopAgent`) and `booking_execution_agent` (Human-in-the-Loop gated).
- `app`: Resumable ADK `App` registering `TravelSafetyPolicyPlugin`,
  `ObservabilityPlugin`, `EventsCompactionConfig`, and `ContextCacheConfig`.
- `create_persistent_runner()`: Production helper binding `app` to SQLite /
  Cloud SQL `DatabaseSessionService`.
"""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.adk.apps.app import App, ResumabilityConfig
from google.adk.runners import Runner

from travel_concierge.config import apply_vertex_ai_location_override, load_settings
from travel_concierge.database import get_catalog_db_path
from travel_concierge.guardrails import TravelSafetyPolicyPlugin
from travel_concierge.memory import (
    build_context_cache_config,
    build_events_compaction_config,
    consolidate_traveler_memory_callback,
    create_persistent_session_service,
    hydrate_traveler_memory_callback,
)
from travel_concierge.observability import (
    ObservabilityPlugin,
    configure_distributed_tracing,
    configure_structured_logging,
)
from travel_concierge.prompts import COORDINATOR_INSTRUCTION
from travel_concierge.sub_agents import (
    build_booking_execution_agent,
    build_trip_planning_pipeline,
)
from travel_concierge.tools import (
    calculate_itinerary_budget_breakdown,
    load_traveler_preferences,
    retrieve_destination_advisory,
    save_traveler_preference_note,
    search_scheduled_flights,
    search_vetted_hotels,
)

# Initialize Vertex AI location, structured JSON logging, OTel tracer, and the
# seeded SQLite catalog on load.
apply_vertex_ai_location_override()
configure_structured_logging()
configure_distributed_tracing()
get_catalog_db_path()


def build_root_agent() -> LlmAgent:
    """Assemble the TripPilot multi-agent graph with strategic model routing."""
    settings = load_settings()
    return LlmAgent(
        name="travel_concierge_coordinator",
        model=settings.models.select_model_for_task("coordinator_routing"),
        description=(
            "TripPilot Budget-Smart Travel Concierge coordinating travel research, "
            "budget optimization, preference memory, and HITL-confirmed bookings."
        ),
        instruction=COORDINATOR_INSTRUCTION,
        tools=[
            load_traveler_preferences,
            save_traveler_preference_note,
            search_scheduled_flights,
            search_vetted_hotels,
            retrieve_destination_advisory,
            calculate_itinerary_budget_breakdown,
        ],
        sub_agents=[
            build_trip_planning_pipeline(),
            build_booking_execution_agent(),
        ],
        before_agent_callback=hydrate_traveler_memory_callback,
        after_agent_callback=consolidate_traveler_memory_callback,
    )


root_agent = build_root_agent()

app = App(
    name="travel_concierge",
    root_agent=root_agent,
    plugins=[
        TravelSafetyPolicyPlugin(),
        ObservabilityPlugin(),
    ],
    events_compaction_config=build_events_compaction_config(),
    context_cache_config=build_context_cache_config(),
    resumability_config=ResumabilityConfig(is_resumable=True),
)


def create_persistent_runner(
    db_url: str | None = None,
    *,
    auto_create_session: bool = True,
) -> Runner:
    """Create a stateful ADK `Runner` backed by persistent `DatabaseSessionService`."""
    session_service = create_persistent_session_service(db_url=db_url)
    return Runner(
        app=app,
        session_service=session_service,
        auto_create_session=auto_create_session,
    )
