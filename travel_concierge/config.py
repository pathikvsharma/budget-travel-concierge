"""Centralized configuration, secure secret loading, and strategic model routing.

Loads runtime configuration strictly from environment variables, Application
Default Credentials (ADC), Google Cloud Secret Manager (when configured), or a
local gitignored `.env` file. Contains zero hardcoded credentials or project IDs.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re

from dotenv import load_dotenv

# Load local `.env` if present without overriding explicit environment variables.
load_dotenv(override=False)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _parse_bool(raw: str | None, default: bool = False) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _parse_float(raw: str | None, default: float, minimum: float) -> float:
    if raw is None or not raw.strip():
        return default
    try:
        parsed = float(raw)
    except ValueError:
        return default
    return max(parsed, minimum)


def _parse_int(raw: str | None, default: int, minimum: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return max(parsed, minimum)


def resolve_safe_data_dir(raw_dir: str | None = None) -> Path:
    """Resolve and enforce safe directory bounds for local SQLite storage.

    Prevents path traversal when resolving `TRAVEL_DATA_DIR` relative paths.
    Absolute test paths inside OS temporary directories remain permitted for
    isolated pytest fixtures while traversal sequences are stripped via resolve.
    """
    configured = (raw_dir or os.getenv("TRAVEL_DATA_DIR", "./data")).strip()
    candidate = Path(configured)
    if not candidate.is_absolute():
        resolved = (_PROJECT_ROOT / candidate).resolve()
        # Enforce strict project-root boundary with Path.is_relative_to (handles trailing slash safely).
        if not resolved.is_relative_to(_PROJECT_ROOT):
            raise ValueError(
                "Invalid TRAVEL_DATA_DIR: relative paths must stay within the repository root."
            )
    else:
        resolved = candidate.resolve()

    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def maybe_load_api_key_from_secret_manager() -> bool:
    """Populate `GOOGLE_API_KEY` from GCP Secret Manager when explicitly configured.

    Never logs secret material and fails closed without crashing offline tests
    if Secret Manager is unreachable when Vertex AI ADC is used instead.
    """
    if os.getenv("GOOGLE_API_KEY"):
        return True

    secret_resource = os.getenv("GCP_SECRET_MANAGER_API_KEY_RESOURCE", "").strip()
    if not secret_resource:
        return False

    try:
        from google.cloud import secretmanager

        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(request={"name": secret_resource})
        secret_payload = response.payload.data.decode("utf-8").strip()
        if secret_payload:
            os.environ["GOOGLE_API_KEY"] = secret_payload
            return True
    except Exception:
        logging.getLogger(__name__).error(
            "Failed to access configured Secret Manager resource; falling back to ADC check."
        )
    return False


_VERTEX_LOCATION_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,30}$")


def apply_vertex_ai_location_override() -> None:
    """Apply `VERTEX_AI_LOCATION` to `GOOGLE_CLOUD_LOCATION` for Gemini calls.

    `adk deploy cloud_run` bakes the Cloud Run region into
    `GOOGLE_CLOUD_LOCATION`, but Gemini 3.x models (including the default
    `gemini-3.1-pro-preview`) are only served from Vertex AI's `global`
    endpoint. Setting `VERTEX_AI_LOCATION=global` keeps Cloud Run in a regional
    location while model calls use the global endpoint. Invalid values are
    ignored (allow-listed format) so a malformed setting cannot redirect calls.
    """
    override = os.getenv("VERTEX_AI_LOCATION", "").strip().lower()
    if not override:
        return
    if not _VERTEX_LOCATION_PATTERN.fullmatch(override):
        logging.getLogger(__name__).warning(
            "Ignoring VERTEX_AI_LOCATION with an invalid format; expected e.g. 'global'."
        )
        return
    os.environ["GOOGLE_CLOUD_LOCATION"] = override


@dataclass(frozen=True)
class ModelRoutingConfig:
    """Tiered Gemini model selection balancing latency, cost, and reasoning depth."""

    fast_model: str
    reasoning_model: str

    def select_model_for_task(
        self, task_kind: str
    ) -> str:
        """Route each task category to the appropriate Gemini tier.

        - `fast` tier (default `gemini-3.5-flash`): Coordinator triage, concurrent
          catalog retrieval (`ParallelAgent`), rule-based compliance checks, and
          booking I/O.
        - `reasoning` tier (default `gemini-3.1-pro-preview`): Multi-constraint
          itinerary synthesis, budget trade-off optimization, and complex
          multi-city planning.
        """
        high_reasoning_tasks = {
            "itinerary_synthesis",
            "budget_tradeoff_optimization",
            "complex_replanning",
        }
        if task_kind in high_reasoning_tasks:
            return self.reasoning_model
        return self.fast_model


@dataclass(frozen=True)
class AppSettings:
    """Immutable runtime settings for TripPilot."""

    app_name: str
    service_name: str
    log_level: str
    use_vertex_ai: bool
    gcp_project: str | None
    gcp_location: str
    models: ModelRoutingConfig
    data_dir: Path
    catalog_db_path: Path
    session_db_url: str
    compaction_interval: int
    compaction_overlap_size: int
    max_context_turns: int
    hitl_booking_threshold_usd: float
    max_single_booking_usd: float
    enable_cloud_trace: bool


def load_settings() -> AppSettings:
    """Load application settings from environment variables and Secret Manager."""
    maybe_load_api_key_from_secret_manager()

    data_dir = resolve_safe_data_dir()
    catalog_db_path = (data_dir / "trippilot_catalog.db").resolve()
    if not catalog_db_path.is_relative_to(data_dir):
        raise ValueError("Resolved catalog database path escaped data directory boundary.")

    default_session_db = (data_dir / "trippilot_sessions.db").resolve()
    session_db_url = os.getenv(
        "TRAVEL_SESSION_DB_URL",
        f"sqlite+aiosqlite:///{default_session_db.as_posix()}",
    ).strip()

    models = ModelRoutingConfig(
        fast_model=os.getenv("FAST_MODEL", "gemini-3.5-flash").strip(),
        reasoning_model=os.getenv("REASONING_MODEL", "gemini-3.1-pro-preview").strip(),
    )

    return AppSettings(
        app_name="travel_concierge",
        service_name=os.getenv("SERVICE_NAME", "trippilot-travel-concierge").strip(),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
        use_vertex_ai=_parse_bool(os.getenv("GOOGLE_GENAI_USE_VERTEXAI"), default=False),
        gcp_project=os.getenv("GOOGLE_CLOUD_PROJECT") or None,
        gcp_location=os.getenv("GOOGLE_CLOUD_LOCATION", "global").strip(),
        models=models,
        data_dir=data_dir,
        catalog_db_path=catalog_db_path,
        session_db_url=session_db_url,
        compaction_interval=_parse_int(os.getenv("COMPACTION_INTERVAL"), default=6, minimum=2),
        compaction_overlap_size=_parse_int(
            os.getenv("COMPACTION_OVERLAP_SIZE"), default=2, minimum=1
        ),
        max_context_turns=_parse_int(os.getenv("MAX_CONTEXT_TURNS"), default=16, minimum=4),
        hitl_booking_threshold_usd=_parse_float(
            os.getenv("HITL_BOOKING_THRESHOLD_USD"), default=250.0, minimum=0.0
        ),
        max_single_booking_usd=_parse_float(
            os.getenv("MAX_SINGLE_BOOKING_USD"), default=15000.0, minimum=100.0
        ),
        enable_cloud_trace=_parse_bool(os.getenv("ENABLE_CLOUD_TRACE"), default=False),
    )
