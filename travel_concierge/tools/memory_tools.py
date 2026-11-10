"""Traveler preference recall and persistence tools backed by parameterized SQLite."""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from travel_concierge.database import fetch_preferences_sync, upsert_preference_sync
from travel_concierge.observability import log_structured_event
from travel_concierge.schemas import (
    GuidedToolError,
    SavePreferenceInput,
    TravelerPreferenceRecord,
    TravelerPreferencesInput,
    TravelerPreferencesOutput,
)


def _effective_traveler_id(
    explicit_id: Optional[str], tool_context: Optional[ToolContext]
) -> str:
    """Enforce session ownership so users only read/modify their own preference namespace."""
    if tool_context is not None:
        ctx_uid = str(getattr(tool_context, "user_id", "") or "").strip()
        if ctx_uid:
            sanitized = re.sub(r"[^A-Za-z0-9_.-]", "_", ctx_uid)[:64]
            if sanitized:
                return sanitized
    return (explicit_id or "default_traveler").strip()


def load_traveler_preferences(
    traveler_id: str = "default_traveler",
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Load saved long-term travel preferences for the active traveler profile.

    Retrieves previously stored non-PII preferences (dietary restrictions, seat
    choice, accommodation style, accessibility needs, loyalty programs, and trip
    pacing) across past sessions so itineraries stay personalized automatically.

    Args:
        traveler_id: Fallback alphanumeric profile ID when running outside an
            authenticated session; automatically overridden by the authenticated
            session `user_id` when `tool_context` is present.
        tool_context: Automatically injected ADK `ToolContext`.

    Returns:
        A JSON-serializable dictionary matching `TravelerPreferencesOutput` with
        the list of stored `preferences`, or `GuidedToolError` on failure.
    """
    resolved_id = _effective_traveler_id(traveler_id, tool_context)
    try:
        validated = TravelerPreferencesInput(traveler_id=resolved_id)
    except ValidationError as exc:
        return GuidedToolError(
            error_code="INVALID_TRAVELER_ID",
            message=f"Invalid traveler identifier: {exc.errors()[0].get('msg')}",
            recovery_guidance=(
                "Use an alphanumeric `traveler_id` (2-64 chars, letters/digits/`_`/`.`/`-`)."
            ),
        ).model_dump()

    try:
        raw_records = fetch_preferences_sync(validated.traveler_id)
    except sqlite3.Error as exc:
        log_structured_event(
            "memory.db_error",
            "Failed reading traveler preferences",
            error_type=type(exc).__name__,
        )
        return GuidedToolError(
            error_code="MEMORY_READ_FAILED",
            message="Traveler preference store is temporarily unreachable.",
            recovery_guidance="Proceed with preferences stated in the current conversation.",
        ).model_dump()

    records = [TravelerPreferenceRecord(**rec) for rec in raw_records]
    msg = (
        f"Loaded {len(records)} saved preference(s)."
        if records
        else "No saved preferences found yet for this traveler."
    )
    return TravelerPreferencesOutput(
        traveler_id=validated.traveler_id,
        preferences=records,
        message=msg,
    ).model_dump()


def save_traveler_preference_note(
    category: str,
    preference_value: str,
    traveler_id: str = "default_traveler",
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Save or update a non-PII long-term preference for the active traveler.

    Automatically scrubs sensitive PII (emails, phones, credit cards, SSNs,
    passports) before writing to persistent SQLite storage.

    Args:
        category: Preference bucket; must be one of `'seat'`, `'dietary'`,
            `'accommodation'`, `'airline_loyalty'`, `'accessibility'`,
            `'pacing'`, or `'budget_style'`.
        preference_value: Concise non-PII preference text (2-240 characters),
            such as `'Vegetarian meals only'` or `'Window seat, nonstop preferred'`.
        traveler_id: Fallback profile ID overridden by the session `user_id`
            when `tool_context` is supplied to enforce resource ownership.
        tool_context: Automatically injected ADK `ToolContext`.

    Returns:
        A JSON-serializable dictionary matching `TravelerPreferencesOutput`
        confirming the stored record, or `GuidedToolError` with valid categories.
    """
    allowed_categories = [
        "seat",
        "dietary",
        "accommodation",
        "airline_loyalty",
        "accessibility",
        "pacing",
        "budget_style",
    ]
    resolved_id = _effective_traveler_id(traveler_id, tool_context)
    try:
        validated = SavePreferenceInput(
            traveler_id=resolved_id,
            category=category,  # type: ignore[arg-type]
            preference_value=preference_value,
        )
    except ValidationError as exc:
        return GuidedToolError(
            error_code="INVALID_PREFERENCE_INPUT",
            message=f"Preference validation failed: {exc.errors()[0].get('msg')}",
            recovery_guidance=(
                "Retry `save_traveler_preference_note` using one of the supported "
                "categories in `supported_values` and a non-empty summary <= 240 chars."
            ),
            supported_values=allowed_categories,
        ).model_dump()

    try:
        saved = upsert_preference_sync(
            traveler_id=validated.traveler_id,
            category=validated.category,
            preference_value=validated.preference_value,
            source="explicit_tool",
        )
    except sqlite3.Error as exc:
        log_structured_event(
            "memory.db_error",
            "Failed saving traveler preference",
            error_type=type(exc).__name__,
        )
        return GuidedToolError(
            error_code="MEMORY_WRITE_FAILED",
            message="Unable to store traveler preference right now.",
            recovery_guidance="Keep the preference in conversation context for this turn.",
        ).model_dump()

    if tool_context is not None:
        all_prefs = fetch_preferences_sync(validated.traveler_id)
        tool_context.state["traveler_memory_summary"] = "; ".join(
            f"{p['category']}={p['preference_value']}" for p in all_prefs
        )

    return TravelerPreferencesOutput(
        traveler_id=validated.traveler_id,
        preferences=[TravelerPreferenceRecord(**saved)],
        message=f"Saved '{validated.category}' preference for future sessions.",
    ).model_dump()
