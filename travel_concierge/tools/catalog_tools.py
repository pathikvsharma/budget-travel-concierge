"""Catalog lookup, advisory retrieval, budget breakdown, and loop approval tools.

All tools enforce strict Pydantic input/output JSON schemas, parameterized SQLite
queries, descriptive action-oriented names, and guided error recovery messages.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from travel_concierge.database import (
    SUPPORTED_BOOKABLE_DESTINATIONS,
    get_db_connection,
    normalize_location_code,
)
from travel_concierge.observability import log_structured_event
from travel_concierge.schemas import (
    BudgetBreakdownInput,
    BudgetBreakdownOutput,
    DestinationAdvisory,
    DestinationAdvisoryInput,
    DestinationAdvisoryOutput,
    FlightOption,
    FlightSearchInput,
    FlightSearchOutput,
    GuidedToolError,
    HotelOption,
    HotelSearchInput,
    HotelSearchOutput,
)

LODGING_TAX_AND_FEES_MULTIPLIER = 1.14  # 14% hospitality tax & local levy estimate


def _format_validation_error(exc: ValidationError, guidance: str) -> dict[str, Any]:
    first = exc.errors()[0] if exc.errors() else {"loc": ("input",), "msg": "Invalid input"}
    field_name = ".".join(str(part) for part in first.get("loc", ()))
    return GuidedToolError(
        error_code="INVALID_TOOL_ARGUMENTS",
        message=f"Validation failed on '{field_name}': {first.get('msg', 'invalid value')}.",
        recovery_guidance=guidance,
    ).model_dump()


def search_scheduled_flights(
    origin: str,
    destination: str,
    travelers: int = 1,
    cabin_class: Optional[str] = None,
    max_roundtrip_usd: Optional[float] = None,
) -> dict[str, Any]:
    """Search vetted roundtrip flight listings between an origin hub and destination.

    Queries the curated flight catalog using validated IATA location codes and
    returns sorted roundtrip fare options with per-person and total group pricing.

    Args:
        origin: Departure airport/city IATA code or city name (supported hubs with
            outbound schedules: 'SFO', 'NYC', 'LAX').
        destination: Arrival airport/city IATA code or city name (supported
            destinations: 'LIS', 'LON', 'MEX', 'NYC', 'PAR', 'SFO', 'TYO').
        travelers: Total number of passengers traveling together (integer 1 to 9).
        cabin_class: Optional cabin filter; must be one of 'economy',
            'premium_economy', or 'business' when supplied.
        max_roundtrip_usd: Optional maximum roundtrip fare per person in USD.

    Returns:
        A JSON-serializable dictionary matching `FlightSearchOutput` on success
        (`status`, `origin_code`, `destination_code`, `matching_count`,
        `cheapest_roundtrip_usd`, `flights`), or `GuidedToolError` with
        actionable `recovery_guidance` when validation or route lookup fails.
    """
    try:
        validated = FlightSearchInput(
            origin=origin,
            destination=destination,
            travelers=travelers,
            cabin_class=cabin_class,  # type: ignore[arg-type]
            max_roundtrip_usd=max_roundtrip_usd,
        )
    except ValidationError as exc:
        return _format_validation_error(
            exc,
            "Pass a non-empty origin/destination, travelers between 1 and 9, "
            "and cabin_class in ['economy', 'premium_economy', 'business'] or omit it.",
        )

    origin_code = normalize_location_code(validated.origin)
    if not origin_code or origin_code not in {"SFO", "NYC", "LAX"}:
        return GuidedToolError(
            error_code="UNSUPPORTED_ORIGIN_HUB",
            message=f"Origin '{validated.origin}' has no direct catalog schedules.",
            recovery_guidance=(
                "Retry `search_scheduled_flights` using one of the supported "
                "departure hubs ('SFO', 'NYC', or 'LAX') closest to the traveler."
            ),
            supported_values=["SFO", "NYC", "LAX"],
        ).model_dump()

    dest_code = normalize_location_code(validated.destination)
    if not dest_code:
        return GuidedToolError(
            error_code="UNSUPPORTED_DESTINATION",
            message=f"Destination '{validated.destination}' is not in the vetted catalog.",
            recovery_guidance=(
                "Retry `search_scheduled_flights` with a supported destination code "
                "from `supported_values`, or ask the traveler to choose among them."
            ),
            supported_values=list(SUPPORTED_BOOKABLE_DESTINATIONS),
        ).model_dump()

    if dest_code == "KBL":
        return GuidedToolError(
            error_code="SAFETY_LEVEL_4_DO_NOT_TRAVEL",
            message="Flights to 'KBL' are blocked under Level-4 Do Not Travel policy.",
            recovery_guidance=(
                "Explain the Level-4 safety restriction and recommend a Level 1-2 "
                "destination from `supported_values`."
            ),
            supported_values=list(SUPPORTED_BOOKABLE_DESTINATIONS),
        ).model_dump()

    if origin_code == dest_code:
        return GuidedToolError(
            error_code="IDENTICAL_ORIGIN_AND_DESTINATION",
            message=f"Origin and destination both resolved to '{origin_code}'.",
            recovery_guidance="Choose a different origin hub or destination city and retry.",
            supported_values=list(SUPPORTED_BOOKABLE_DESTINATIONS),
        ).model_dump()

    try:
        query = """
            SELECT flight_id, carrier, origin_code, destination_code, cabin_class,
                   roundtrip_fare_usd, duration_hours, stops, refundable, baggage_included
            FROM flights
            WHERE origin_code = ? AND destination_code = ?
        """
        params: list[Any] = [origin_code, dest_code]
        if validated.cabin_class is not None:
            query += " AND cabin_class = ?"
            params.append(validated.cabin_class)
        if validated.max_roundtrip_usd is not None:
            query += " AND roundtrip_fare_usd <= ?"
            params.append(validated.max_roundtrip_usd)
        query += " ORDER BY roundtrip_fare_usd ASC"

        with get_db_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
    except sqlite3.Error as exc:
        log_structured_event(
            "catalog.db_error",
            "Database error during flight lookup",
            error_type=type(exc).__name__,
        )
        return GuidedToolError(
            error_code="CATALOG_UNAVAILABLE",
            message="Flight catalog lookup temporarily failed.",
            recovery_guidance="Retry `search_scheduled_flights` once without optional filters.",
        ).model_dump()

    options = [
        FlightOption(
            flight_id=row["flight_id"],
            carrier=row["carrier"],
            origin_code=row["origin_code"],
            destination_code=row["destination_code"],
            cabin_class=row["cabin_class"],
            roundtrip_fare_usd=float(row["roundtrip_fare_usd"]),
            total_fare_for_group_usd=round(
                float(row["roundtrip_fare_usd"]) * validated.travelers, 2
            ),
            duration_hours=float(row["duration_hours"]),
            stops=int(row["stops"]),
            refundable=bool(row["refundable"]),
            baggage_included=row["baggage_included"],
        )
        for row in rows
    ]

    note = ""
    if not options:
        note = (
            f"No flights matched from {origin_code} to {dest_code} with the current "
            "cabin/price filters. Retry omitting `max_roundtrip_usd` or `cabin_class`, "
            "or try hub 'SFO' or 'NYC'."
        )

    return FlightSearchOutput(
        origin_code=origin_code,
        destination_code=dest_code,
        travelers=validated.travelers,
        matching_count=len(options),
        cheapest_roundtrip_usd=options[0].roundtrip_fare_usd if options else None,
        flights=options,
        note=note,
    ).model_dump()


def search_vetted_hotels(
    destination: str,
    nights: int,
    max_nightly_usd: Optional[float] = None,
    tier: Optional[str] = None,
) -> dict[str, Any]:
    """Search vetted hotels and guesthouses at a destination for a given stay length.

    Args:
        destination: Target destination IATA code or city name (e.g. 'LIS',
            'Tokyo', 'PAR', 'MEX', 'LON', 'NYC', 'SFO').
        nights: Number of lodging nights required (integer 1 to 30).
        max_nightly_usd: Optional nightly ceiling in USD before 14% lodging tax.
        tier: Optional property category filter ('budget', 'boutique', 'luxury').

    Returns:
        A JSON-serializable dictionary matching `HotelSearchOutput` with pre-tax
        nightly rates, stay subtotals, breakfast/cancellation flags, and
        accessibility details, or `GuidedToolError` with recovery instructions.
    """
    try:
        validated = HotelSearchInput(
            destination=destination,
            nights=nights,
            max_nightly_usd=max_nightly_usd,
            tier=tier,  # type: ignore[arg-type]
        )
    except ValidationError as exc:
        return _format_validation_error(
            exc,
            "Provide a valid destination, `nights` between 1 and 30, and optional "
            "`tier` in ['budget', 'boutique', 'luxury'].",
        )

    dest_code = normalize_location_code(validated.destination)
    if not dest_code or dest_code not in SUPPORTED_BOOKABLE_DESTINATIONS:
        if dest_code == "KBL":
            return GuidedToolError(
                error_code="SAFETY_LEVEL_4_DO_NOT_TRAVEL",
                message="Lodging search for 'KBL' is blocked by Level-4 safety policy.",
                recovery_guidance="Choose a Level 1-2 destination from `supported_values`.",
                supported_values=list(SUPPORTED_BOOKABLE_DESTINATIONS),
            ).model_dump()
        return GuidedToolError(
            error_code="UNSUPPORTED_DESTINATION",
            message=f"No vetted hotel catalog exists for '{validated.destination}'.",
            recovery_guidance=(
                "Retry `search_vetted_hotels` using one of the supported city codes "
                "listed in `supported_values`."
            ),
            supported_values=list(SUPPORTED_BOOKABLE_DESTINATIONS),
        ).model_dump()

    try:
        query = """
            SELECT hotel_id, name, destination_code, neighborhood, tier,
                   nightly_rate_usd, rating, breakfast_included,
                   free_cancellation, accessibility_features
            FROM hotels
            WHERE destination_code = ?
        """
        params: list[Any] = [dest_code]
        if validated.tier is not None:
            query += " AND tier = ?"
            params.append(validated.tier)
        if validated.max_nightly_usd is not None:
            query += " AND nightly_rate_usd <= ?"
            params.append(validated.max_nightly_usd)
        query += " ORDER BY nightly_rate_usd ASC"

        with get_db_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
    except sqlite3.Error as exc:
        log_structured_event(
            "catalog.db_error",
            "Database error during hotel lookup",
            error_type=type(exc).__name__,
        )
        return GuidedToolError(
            error_code="CATALOG_UNAVAILABLE",
            message="Hotel catalog lookup temporarily failed.",
            recovery_guidance="Retry `search_vetted_hotels` without optional filters.",
        ).model_dump()

    hotels = [
        HotelOption(
            hotel_id=row["hotel_id"],
            name=row["name"],
            destination_code=row["destination_code"],
            neighborhood=row["neighborhood"],
            tier=row["tier"],
            nightly_rate_usd=float(row["nightly_rate_usd"]),
            stay_subtotal_usd=round(float(row["nightly_rate_usd"]) * validated.nights, 2),
            rating=float(row["rating"]),
            breakfast_included=bool(row["breakfast_included"]),
            free_cancellation=bool(row["free_cancellation"]),
            accessibility_features=row["accessibility_features"],
        )
        for row in rows
    ]

    note = (
        "Note: Lodging subtotals exclude estimated 14% hospitality taxes/fees; "
        "call `calculate_itinerary_budget_breakdown` for the exact tax-inclusive total."
    )
    if not hotels:
        note = (
            f"No hotels in {dest_code} matched `max_nightly_usd={validated.max_nightly_usd}` "
            f"and `tier={validated.tier}`. Retry omitting `max_nightly_usd` and `tier` "
            "to inspect the cheapest available 'budget' property."
        )

    return HotelSearchOutput(
        destination_code=dest_code,
        nights=validated.nights,
        matching_count=len(hotels),
        lowest_nightly_usd=hotels[0].nightly_rate_usd if hotels else None,
        hotels=hotels,
        note=note,
    ).model_dump()


def retrieve_destination_advisory(destination: str) -> dict[str, Any]:
    """Retrieve visa/entry rules, safety level (1-4), climate, and daily local costs.

    Args:
        destination: Destination IATA code or city name (e.g. 'TYO', 'Lisbon',
            'PAR', 'MEX', 'LON', 'NYC', 'SFO', 'KBL').

    Returns:
        A JSON-serializable dictionary matching `DestinationAdvisoryOutput` with
        safety level, visa requirements, seasonal weather, daily meals/transit
        budget norm in USD, and `booking_permitted`, or `GuidedToolError`.
    """
    try:
        validated = DestinationAdvisoryInput(destination=destination)
    except ValidationError as exc:
        return _format_validation_error(
            exc, "Pass a non-empty destination city name or 3-letter IATA code."
        )

    dest_code = normalize_location_code(validated.destination)
    if not dest_code:
        return GuidedToolError(
            error_code="ADVISORY_NOT_FOUND",
            message=f"No travel advisory record found for '{validated.destination}'.",
            recovery_guidance=(
                "Retry `retrieve_destination_advisory` using one of the supported "
                "destination codes in `supported_values`."
            ),
            supported_values=[*SUPPORTED_BOOKABLE_DESTINATIONS, "KBL"],
        ).model_dump()

    try:
        with get_db_connection() as conn:
            row = conn.execute(
                """
                SELECT destination_code, city, country, currency_code, safety_level,
                       visa_and_entry_notes, seasonal_weather_summary,
                       avg_daily_meals_and_transit_usd, safety_guidance, booking_permitted
                FROM advisories
                WHERE destination_code = ?
                """,
                (dest_code,),
            ).fetchone()
    except sqlite3.Error as exc:
        log_structured_event(
            "catalog.db_error",
            "Database error during advisory lookup",
            error_type=type(exc).__name__,
        )
        return GuidedToolError(
            error_code="ADVISORY_STORE_UNAVAILABLE",
            message="Destination advisory store temporarily unavailable.",
            recovery_guidance="Retry `retrieve_destination_advisory` once.",
        ).model_dump()

    if row is None:
        return GuidedToolError(
            error_code="ADVISORY_NOT_FOUND",
            message=f"Advisory record missing for destination code '{dest_code}'.",
            recovery_guidance="Choose a destination from `supported_values`.",
            supported_values=list(SUPPORTED_BOOKABLE_DESTINATIONS),
        ).model_dump()

    advisory = DestinationAdvisory(
        destination_code=row["destination_code"],
        city=row["city"],
        country=row["country"],
        currency_code=row["currency_code"],
        safety_level=int(row["safety_level"]),
        visa_and_entry_notes=row["visa_and_entry_notes"],
        seasonal_weather_summary=row["seasonal_weather_summary"],
        avg_daily_meals_and_transit_usd=float(row["avg_daily_meals_and_transit_usd"]),
        safety_guidance=row["safety_guidance"],
        booking_permitted=bool(row["booking_permitted"]),
    )
    return DestinationAdvisoryOutput(advisory=advisory).model_dump()


def calculate_itinerary_budget_breakdown(
    target_budget_usd: float,
    flight_roundtrip_per_person_usd: float,
    hotel_nightly_rate_usd: float,
    nights: int,
    daily_local_spend_per_person_usd: float,
    travelers: int = 1,
    contingency_pct: float = 8.0,
) -> dict[str, Any]:
    """Compute an exact, tax-inclusive itinerary budget breakdown and compliance check.

    Calculates group airfare, lodging with 14% hospitality taxes, daily meals and
    transit across `nights + 1` travel days, and an emergency contingency buffer,
    comparing the grand total against `target_budget_usd`.

    Args:
        target_budget_usd: Traveler's maximum total budget ceiling in USD (> 0).
        flight_roundtrip_per_person_usd: Roundtrip fare per traveler in USD.
        hotel_nightly_rate_usd: Total pre-tax nightly lodging rate in USD.
        nights: Total hotel nights (integer 1 to 30).
        daily_local_spend_per_person_usd: Daily meals/transit/activities spend per
            traveler in USD (typically from `retrieve_destination_advisory`).
        travelers: Number of travelers sharing the trip (integer 1 to 9).
        contingency_pct: Contingency reserve percentage between 0 and 30 (default 8.0).

    Returns:
        A JSON-serializable dictionary matching `BudgetBreakdownOutput` including
        `estimated_grand_total_usd`, `remaining_headroom_usd`, `within_budget`,
        and prioritized `optimization_recommendations`.
    """
    try:
        validated = BudgetBreakdownInput(
            target_budget_usd=target_budget_usd,
            flight_roundtrip_per_person_usd=flight_roundtrip_per_person_usd,
            hotel_nightly_rate_usd=hotel_nightly_rate_usd,
            nights=nights,
            daily_local_spend_per_person_usd=daily_local_spend_per_person_usd,
            travelers=travelers,
            contingency_pct=contingency_pct,
        )
    except ValidationError as exc:
        return _format_validation_error(
            exc,
            "Ensure `target_budget_usd > 0`, `nights` is 1..30, `travelers` is 1..9, "
            "and all USD rates are non-negative numbers.",
        )

    flights_total = round(
        validated.flight_roundtrip_per_person_usd * validated.travelers, 2
    )
    lodging_with_tax = round(
        validated.hotel_nightly_rate_usd * validated.nights * LODGING_TAX_AND_FEES_MULTIPLIER,
        2,
    )
    active_days = validated.nights
    local_total = round(
        validated.daily_local_spend_per_person_usd * active_days * validated.travelers,
        2,
    )
    base_subtotal = flights_total + lodging_with_tax + local_total
    contingency = round(base_subtotal * (validated.contingency_pct / 100.0), 2)
    grand_total = round(base_subtotal + contingency, 2)
    headroom = round(validated.target_budget_usd - grand_total, 2)
    within_budget = grand_total <= validated.target_budget_usd
    utilization = round((grand_total / validated.target_budget_usd) * 100.0, 1)

    recommendations: list[str] = []
    if not within_budget:
        overage = round(grand_total - validated.target_budget_usd, 2)
        recommendations.append(
            f"Itinerary exceeds target budget by ${overage:,.2f}; reduce cost before booking."
        )
        if validated.hotel_nightly_rate_usd > 140:
            recommendations.append(
                "Switch accommodation from luxury/boutique to a vetted 'budget' tier property."
            )
        if validated.flight_roundtrip_per_person_usd > 700:
            recommendations.append(
                "Downgrade cabin to 'economy' or compare alternative departure hub ('NYC' vs 'SFO')."
            )
        if validated.nights > 3:
            recommendations.append(
                "Shorten the stay by 1-2 nights or choose a lower-cost city like LIS or MEX."
            )
    else:
        recommendations.append(
            f"Plan fits comfortably within budget with ${headroom:,.2f} unused headroom."
        )

    return BudgetBreakdownOutput(
        target_budget_usd=round(validated.target_budget_usd, 2),
        flights_total_usd=flights_total,
        lodging_with_taxes_usd=lodging_with_tax,
        local_expenses_total_usd=local_total,
        contingency_buffer_usd=contingency,
        estimated_grand_total_usd=grand_total,
        remaining_headroom_usd=headroom,
        within_budget=within_budget,
        utilization_pct=utilization,
        optimization_recommendations=recommendations,
    ).model_dump()


def approve_budget_compliant_plan(
    compliance_summary: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Certify that the draft itinerary satisfies budget and safety rules and exit the loop.

    Invoked exclusively by `budget_compliance_critic_agent` inside
    `budget_optimization_loop` once the drafted itinerary uses verified catalog IDs,
    respects safety advisories, and stays within budget (or documents the honest
    minimum-viable catalog floor). Sets `tool_context.actions.escalate = True` to
    terminate iterative refinement cleanly.

    Args:
        compliance_summary: One-paragraph audit summary confirming total USD cost
            vs. target budget, selected `flight_id` and `hotel_id`, and safety level.
        tool_context: ADK runtime tool context injected automatically.

    Returns:
        Confirmation dictionary recording loop termination and audit verdict.
    """
    cleaned = compliance_summary.strip()
    if len(cleaned) < 10:
        return GuidedToolError(
            error_code="EMPTY_COMPLIANCE_SUMMARY",
            message="Compliance summary must concisely state the audited cost and IDs.",
            recovery_guidance=(
                "Provide a brief summary citing the audited grand total, target budget, "
                "and selected flight/hotel IDs."
            ),
        ).model_dump()

    tool_context.state["approved_compliance_summary"] = cleaned
    tool_context.actions.escalate = True
    return {
        "status": "success",
        "loop_action": "approved_and_escalated",
        "compliance_summary": cleaned,
    }
