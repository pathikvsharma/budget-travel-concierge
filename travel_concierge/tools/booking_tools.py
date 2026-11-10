"""Human-in-the-Loop (HITL) booking and cancellation tools for TripPilot.

High-value itinerary commitments (`reserve_approved_itinerary`) and destructive
cancellations (`cancel_confirmed_reservation`) are wrapped with ADK's
`FunctionTool(..., require_confirmation=...)` so execution pauses for explicit
human approval before committing funds or mutating reservations.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
import re
import sqlite3
from typing import Any, Optional

from google.adk.tools import FunctionTool
from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from travel_concierge.config import load_settings
from travel_concierge.database import generate_reservation_id, get_db_connection
from travel_concierge.observability import log_structured_event
from travel_concierge.pii import redact_pii_text
from travel_concierge.schemas import (
    BookingActionOutput,
    BookingRecord,
    BookingRequestInput,
    CancelReservationInput,
    GuidedToolError,
)
from travel_concierge.tools.catalog_tools import LODGING_TAX_AND_FEES_MULTIPLIER


def _effective_owner_id(fallback_id: str, tool_context: Optional[ToolContext]) -> str:
    """Bind booking ownership strictly to the authenticated ADK session user."""
    if tool_context is not None:
        ctx_uid = str(getattr(tool_context, "user_id", "") or "").strip()
        if ctx_uid:
            cleaned = re.sub(r"[^A-Za-z0-9_.-]", "_", ctx_uid)[:64]
            if cleaned:
                return cleaned
    return fallback_id


def requires_booking_confirmation(
    approved_budget_usd: float | None = None,
    tool_context: Optional[ToolContext] = None,
    **_other_tool_args: Any,
) -> bool:
    """Dynamic HITL predicate pausing high-stakes itinerary bookings for approval.

    ADK calls this predicate with only the arguments the model supplied plus the
    injected `tool_context`, so it accepts arbitrary keyword arguments. It fails
    closed: a missing, non-numeric, or non-finite `approved_budget_usd` always
    requires human confirmation. Otherwise confirmation is required whenever the
    approved budget meets or exceeds `HITL_BOOKING_THRESHOLD_USD` (default $250,
    which covers virtually all multi-day flight+hotel commitments while staying
    configurable via env).

    Args:
        approved_budget_usd: Traveler-approved booking ceiling in USD, as
            supplied by the model for `reserve_approved_itinerary`.
        tool_context: ADK-injected `ToolContext` (unused; accepted for
            compatibility with ADK's confirmation call).
        **_other_tool_args: Remaining `reserve_approved_itinerary` arguments,
            ignored by the threshold check.

    Returns:
        True when the booking must pause for explicit human confirmation.
    """
    _ = (tool_context, _other_tool_args)
    try:
        budget = float(approved_budget_usd)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return True
    if not math.isfinite(budget):
        return True
    return budget >= load_settings().hitl_booking_threshold_usd


def reserve_approved_itinerary(
    flight_id: str,
    hotel_id: str,
    departure_date: str,
    nights: int,
    approved_budget_usd: float,
    travelers: int = 1,
    traveler_id: str = "default_traveler",
    special_requests: str = "",
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Commit a verified flight + hotel itinerary after Human-in-the-Loop confirmation.

    Validates catalog IDs, verifies destination match and Level-4 safety status,
    checks that total flight + tax-inclusive lodging stays within both
    `approved_budget_usd` and the global policy ceiling, and persists a confirmed
    `TRV-XXXXXXXX` booking record.

    Args:
        flight_id: Exact catalog flight identifier (`FL-XXX-NNN`, e.g. `'FL-LIS-101'`).
        hotel_id: Exact catalog hotel identifier (`HT-XXX-NNN`, e.g. `'HT-LIS-101'`).
        departure_date: Outbound travel date in ISO-8601 `'YYYY-MM-DD'` format.
        nights: Number of hotel nights to reserve (integer 1 to 30).
        approved_budget_usd: Traveler-approved ceiling in USD covering airfare
            and tax-inclusive lodging.
        travelers: Number of passengers included in the booking (integer 1 to 9).
        traveler_id: Fallback traveler identifier overridden by session `user_id`.
        special_requests: Optional non-PII dietary or accessibility request notes.
        tool_context: Automatically injected ADK `ToolContext` supplying HITL
            confirmation status and session identity.

    Returns:
        A JSON-serializable dictionary matching `BookingActionOutput` with the
        confirmed `TRV-XXXXXXXX` reservation on success, or `GuidedToolError`
        detailing exact corrective action on validation or budget failure.
    """
    owner_id = _effective_owner_id(traveler_id, tool_context)
    safe_notes, _ = redact_pii_text(special_requests)

    try:
        validated = BookingRequestInput(
            traveler_id=owner_id,
            flight_id=flight_id.upper(),
            hotel_id=hotel_id.upper(),
            departure_date=departure_date,
            nights=nights,
            travelers=travelers,
            approved_budget_usd=approved_budget_usd,
            special_requests=safe_notes,
        )
    except ValidationError as exc:
        err = exc.errors()[0]
        field_name = ".".join(str(x) for x in err.get("loc", ()))
        return GuidedToolError(
            error_code="INVALID_BOOKING_PARAMETERS",
            message=f"Invalid '{field_name}': {err.get('msg')}",
            recovery_guidance=(
                "Verify `flight_id` matches 'FL-XXX-NNN' from `search_scheduled_flights`, "
                "`hotel_id` matches 'HT-XXX-NNN' from `search_vetted_hotels`, and "
                "`departure_date` uses ISO 'YYYY-MM-DD'."
            ),
        ).model_dump()

    # Defense-in-depth check on explicit ToolConfirmation rejection when running in ADK HITL flow.
    if tool_context is not None:
        confirmation = getattr(tool_context, "tool_confirmation", None)
        if confirmation is not None and not getattr(confirmation, "confirmed", True):
            return GuidedToolError(
                error_code="HUMAN_CONFIRMATION_DECLINED",
                message="The traveler declined confirmation; reservation was NOT created.",
                recovery_guidance=(
                    "Acknowledge that no reservation or charge was made and ask whether "
                    "the traveler wants to adjust dates, hotel tier, or flight selection."
                ),
            ).model_dump()

    settings = load_settings()
    if validated.approved_budget_usd > settings.max_single_booking_usd:
        return GuidedToolError(
            error_code="SPEND_CAP_EXCEEDED",
            message=(
                f"Approved budget ${validated.approved_budget_usd:,.2f} exceeds maximum "
                f"policy cap ${settings.max_single_booking_usd:,.2f}."
            ),
            recovery_guidance=(
                f"Select economy/boutique options below ${settings.max_single_booking_usd:,.2f}."
            ),
        ).model_dump()

    try:
        with get_db_connection() as conn:
            flight_row = conn.execute(
                """
                SELECT flight_id, destination_code, roundtrip_fare_usd
                FROM flights
                WHERE flight_id = ?
                """,
                (validated.flight_id,),
            ).fetchone()
            if flight_row is None:
                return GuidedToolError(
                    error_code="FLIGHT_ID_NOT_FOUND",
                    message=f"Catalog flight '{validated.flight_id}' does not exist.",
                    recovery_guidance=(
                        "Call `search_scheduled_flights` first and pass one of the "
                        "returned `flight_id` values verbatim."
                    ),
                ).model_dump()

            hotel_row = conn.execute(
                """
                SELECT hotel_id, destination_code, nightly_rate_usd
                FROM hotels
                WHERE hotel_id = ?
                """,
                (validated.hotel_id,),
            ).fetchone()
            if hotel_row is None:
                return GuidedToolError(
                    error_code="HOTEL_ID_NOT_FOUND",
                    message=f"Catalog hotel '{validated.hotel_id}' does not exist.",
                    recovery_guidance=(
                        "Call `search_vetted_hotels` first and pass one of the returned "
                        "`hotel_id` values verbatim."
                    ),
                ).model_dump()

            if flight_row["destination_code"] != hotel_row["destination_code"]:
                return GuidedToolError(
                    error_code="DESTINATION_MISMATCH",
                    message=(
                        f"Flight '{validated.flight_id}' arrives in "
                        f"{flight_row['destination_code']} while hotel "
                        f"'{validated.hotel_id}' is in {hotel_row['destination_code']}."
                    ),
                    recovery_guidance=(
                        "Ensure both `flight_id` and `hotel_id` belong to the same "
                        "destination code before retrying `reserve_approved_itinerary`."
                    ),
                ).model_dump()

            dest_code = str(flight_row["destination_code"])
            advisory_row = conn.execute(
                """
                SELECT safety_level, booking_permitted
                FROM advisories
                WHERE destination_code = ?
                """,
                (dest_code,),
            ).fetchone()
            if advisory_row and (
                int(advisory_row["safety_level"]) >= 4
                or not bool(advisory_row["booking_permitted"])
            ):
                return GuidedToolError(
                    error_code="SAFETY_LEVEL_4_DO_NOT_TRAVEL",
                    message=f"Destination '{dest_code}' is blocked from booking for safety.",
                    recovery_guidance="Select a Level 1-2 destination instead.",
                ).model_dump()

            airfare_total = float(flight_row["roundtrip_fare_usd"]) * validated.travelers
            lodging_total = (
                float(hotel_row["nightly_rate_usd"])
                * validated.nights
                * LODGING_TAX_AND_FEES_MULTIPLIER
            )
            committed_usd = round(airfare_total + lodging_total, 2)

            if committed_usd > validated.approved_budget_usd:
                return GuidedToolError(
                    error_code="BOOKING_EXCEEDS_APPROVED_BUDGET",
                    message=(
                        f"Combined airfare + tax-inclusive hotel (${committed_usd:,.2f}) "
                        f"exceeds approved budget (${validated.approved_budget_usd:,.2f})."
                    ),
                    recovery_guidance=(
                        "Choose a cheaper `flight_id` or 'budget' tier `hotel_id`, "
                        "shorten `nights`, or ask the traveler to approve the higher total."
                    ),
                ).model_dump()

            reservation_id = generate_reservation_id()
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO reservations (
                    reservation_id, traveler_id, destination_code, flight_id,
                    hotel_id, departure_date, nights, travelers,
                    total_committed_usd, special_requests, status,
                    cancellation_reason, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CONFIRMED', '', ?, ?)
                """,
                (
                    reservation_id,
                    validated.traveler_id,
                    dest_code,
                    validated.flight_id,
                    validated.hotel_id,
                    validated.departure_date,
                    validated.nights,
                    validated.travelers,
                    committed_usd,
                    validated.special_requests,
                    now_iso,
                    now_iso,
                ),
            )
    except sqlite3.Error as exc:
        log_structured_event(
            "booking.db_error",
            "Database error while writing reservation",
            error_type=type(exc).__name__,
        )
        return GuidedToolError(
            error_code="BOOKING_PERSISTENCE_ERROR",
            message="Reservation ledger could not record the booking.",
            recovery_guidance="Do not assume booking succeeded; inform the traveler and retry once.",
        ).model_dump()

    record = BookingRecord(
        reservation_id=reservation_id,
        traveler_id=validated.traveler_id,
        destination_code=dest_code,
        flight_id=validated.flight_id,
        hotel_id=validated.hotel_id,
        departure_date=validated.departure_date,
        nights=validated.nights,
        travelers=validated.travelers,
        total_committed_usd=committed_usd,
        status="CONFIRMED",
        created_at=now_iso,
    )
    if tool_context is not None:
        tool_context.state["last_reservation_id"] = reservation_id

    return BookingActionOutput(
        booking=record,
        confirmation_message=(
            f"Reservation {reservation_id} confirmed for {dest_code} "
            f"({validated.flight_id} + {validated.hotel_id}, {validated.nights} nights, "
            f"${committed_usd:,.2f} committed including lodging taxes)."
        ),
    ).model_dump()


def cancel_confirmed_reservation(
    reservation_id: str,
    reason: str,
    traveler_id: str = "default_traveler",
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Cancel an existing confirmed itinerary reservation after HITL confirmation.

    Enforces strict resource ownership so travelers can only cancel reservations
    created under their own profile.

    Args:
        reservation_id: Confirmation code in `'TRV-XXXXXXXX'` format.
        reason: Short explanation for the cancellation (3-240 characters).
        traveler_id: Fallback profile ID overridden by session `user_id`.
        tool_context: Automatically injected ADK `ToolContext`.

    Returns:
        A JSON-serializable dictionary matching `BookingActionOutput` with
        `status='CANCELLED'`, or `GuidedToolError` when the code is invalid,
        not owned by the active traveler, or already canceled.
    """
    owner_id = _effective_owner_id(traveler_id, tool_context)
    safe_reason, _ = redact_pii_text(reason)

    try:
        validated = CancelReservationInput(
            reservation_id=reservation_id.upper(),
            reason=safe_reason,
        )
    except ValidationError as exc:
        return GuidedToolError(
            error_code="INVALID_RESERVATION_ID",
            message=f"Validation error: {exc.errors()[0].get('msg')}",
            recovery_guidance=(
                "Ask the traveler for the exact 8-character code formatted as "
                "'TRV-XXXXXXXX' or call `lookup_reservation_status`."
            ),
        ).model_dump()

    if tool_context is not None:
        confirmation = getattr(tool_context, "tool_confirmation", None)
        if confirmation is not None and not getattr(confirmation, "confirmed", True):
            return GuidedToolError(
                error_code="HUMAN_CONFIRMATION_DECLINED",
                message="Cancellation was declined by the traveler; booking remains CONFIRMED.",
                recovery_guidance="Confirm to the traveler that their booking remains active.",
            ).model_dump()

    try:
        with get_db_connection() as conn:
            row = conn.execute(
                """
                SELECT reservation_id, traveler_id, destination_code, flight_id,
                       hotel_id, departure_date, nights, travelers,
                       total_committed_usd, status, created_at
                FROM reservations
                WHERE reservation_id = ? AND traveler_id = ?
                """,
                (validated.reservation_id, owner_id),
            ).fetchone()

            if row is None:
                return GuidedToolError(
                    error_code="RESERVATION_NOT_FOUND",
                    message=(
                        f"No reservation '{validated.reservation_id}' found for the active traveler."
                    ),
                    recovery_guidance=(
                        "Verify the 'TRV-XXXXXXXX' confirmation code with the traveler "
                        "or call `lookup_reservation_status`."
                    ),
                ).model_dump()

            if row["status"] == "CANCELLED":
                return GuidedToolError(
                    error_code="RESERVATION_ALREADY_CANCELLED",
                    message=f"Reservation '{validated.reservation_id}' is already CANCELLED.",
                    recovery_guidance="Let the traveler know no duplicate cancellation is needed.",
                ).model_dump()

            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE reservations
                SET status = 'CANCELLED', cancellation_reason = ?, updated_at = ?
                WHERE reservation_id = ? AND traveler_id = ?
                """,
                (validated.reason, now_iso, validated.reservation_id, owner_id),
            )
    except sqlite3.Error as exc:
        log_structured_event(
            "booking.db_error",
            "Database error while canceling reservation",
            error_type=type(exc).__name__,
        )
        return GuidedToolError(
            error_code="CANCELLATION_PERSISTENCE_ERROR",
            message="Unable to update reservation status at this time.",
            recovery_guidance="Retry `cancel_confirmed_reservation` once.",
        ).model_dump()

    record = BookingRecord(
        reservation_id=row["reservation_id"],
        traveler_id=row["traveler_id"],
        destination_code=row["destination_code"],
        flight_id=row["flight_id"],
        hotel_id=row["hotel_id"],
        departure_date=row["departure_date"],
        nights=int(row["nights"]),
        travelers=int(row["travelers"]),
        total_committed_usd=float(row["total_committed_usd"]),
        status="CANCELLED",
        created_at=row["created_at"],
    )
    return BookingActionOutput(
        booking=record,
        confirmation_message=f"Reservation {validated.reservation_id} has been CANCELLED.",
    ).model_dump()


def lookup_reservation_status(
    reservation_id: str,
    traveler_id: str = "default_traveler",
    tool_context: Optional[ToolContext] = None,
) -> dict[str, Any]:
    """Retrieve the current status and cost details of a traveler's reservation.

    Args:
        reservation_id: Confirmation identifier in `'TRV-XXXXXXXX'` format.
        traveler_id: Fallback profile ID overridden by session `user_id`.
        tool_context: Automatically injected ADK `ToolContext`.

    Returns:
        `BookingActionOutput` dictionary when found for the active traveler, or
        `GuidedToolError` with corrective instructions.
    """
    cleaned_id = reservation_id.strip().upper()
    if not re.fullmatch(r"^TRV-[A-Z0-9]{8}$", cleaned_id):
        return GuidedToolError(
            error_code="INVALID_RESERVATION_ID",
            message="Reservation ID must match format 'TRV-XXXXXXXX'.",
            recovery_guidance="Request a valid 'TRV-XXXXXXXX' code from the traveler.",
        ).model_dump()

    owner_id = _effective_owner_id(traveler_id, tool_context)
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                """
                SELECT reservation_id, traveler_id, destination_code, flight_id,
                       hotel_id, departure_date, nights, travelers,
                       total_committed_usd, status, created_at
                FROM reservations
                WHERE reservation_id = ? AND traveler_id = ?
                """,
                (cleaned_id, owner_id),
            ).fetchone()
    except sqlite3.Error as exc:
        log_structured_event(
            "booking.db_error",
            "Database error during reservation lookup",
            error_type=type(exc).__name__,
        )
        return GuidedToolError(
            error_code="BOOKING_LOOKUP_FAILED",
            message="Reservation lookup failed temporarily.",
            recovery_guidance="Retry `lookup_reservation_status` shortly.",
        ).model_dump()

    if row is None:
        return GuidedToolError(
            error_code="RESERVATION_NOT_FOUND",
            message=f"Reservation '{cleaned_id}' was not found under your profile.",
            recovery_guidance="Double-check the 'TRV-XXXXXXXX' confirmation number.",
        ).model_dump()

    record = BookingRecord(
        reservation_id=row["reservation_id"],
        traveler_id=row["traveler_id"],
        destination_code=row["destination_code"],
        flight_id=row["flight_id"],
        hotel_id=row["hotel_id"],
        departure_date=row["departure_date"],
        nights=int(row["nights"]),
        travelers=int(row["travelers"]),
        total_committed_usd=float(row["total_committed_usd"]),
        status=row["status"],
        created_at=row["created_at"],
    )
    return BookingActionOutput(
        booking=record,
        confirmation_message=f"Reservation {cleaned_id} is currently {row['status']}.",
    ).model_dump()


# Wrap state-mutating high-value tools in ADK HITL confirmation gates.
reserve_approved_itinerary_tool = FunctionTool(
    reserve_approved_itinerary,
    require_confirmation=requires_booking_confirmation,
)

cancel_confirmed_reservation_tool = FunctionTool(
    cancel_confirmed_reservation,
    require_confirmation=True,
)
