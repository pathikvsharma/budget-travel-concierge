"""Explicit Pydantic JSON schemas for all TripPilot tool inputs, outputs, and errors.

Strict schema validation constrains LLM arguments, enforces input allow-lists,
and ensures deterministic JSON tool responses with actionable error guidance.
"""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


CabinClass = Literal["economy", "premium_economy", "business"]
AccommodationTier = Literal["budget", "boutique", "luxury"]


class StrictSchema(BaseModel):
    """Base schema rejecting undeclared fields and stripping leading/trailing whitespace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class GuidedToolError(StrictSchema):
    """Structured error response providing actionable LLM recovery instructions."""

    status: Literal["error"] = "error"
    error_code: str = Field(..., description="Machine-readable error classification.")
    message: str = Field(..., description="Safe human-readable error explanation.")
    recovery_guidance: str = Field(
        ...,
        description="Specific corrective next step the LLM should take to recover.",
    )
    supported_values: list[str] = Field(
        default_factory=list,
        description="Optional list of valid codes or identifiers to retry with.",
    )


# ---------------------------------------------------------------------------
# Flight Search Schemas
# ---------------------------------------------------------------------------
class FlightSearchInput(StrictSchema):
    """Validated input parameters for searching scheduled flights."""

    origin: str = Field(
        ...,
        min_length=2,
        max_length=40,
        description="Origin IATA airport/city code (e.g. 'SFO', 'NYC') or city name.",
    )
    destination: str = Field(
        ...,
        min_length=2,
        max_length=40,
        description="Destination IATA code (e.g. 'TYO', 'LIS', 'PAR') or city name.",
    )
    cabin_class: CabinClass | None = Field(
        default=None,
        description="Optional cabin filter: 'economy', 'premium_economy', or 'business'.",
    )
    max_roundtrip_usd: float | None = Field(
        default=None,
        gt=0,
        le=25000,
        description="Optional maximum roundtrip fare per traveler in USD.",
    )
    travelers: int = Field(
        default=1,
        ge=1,
        le=9,
        description="Number of travelers (1-9) to compute group flight totals.",
    )


class FlightOption(StrictSchema):
    """Single vetted roundtrip flight listing."""

    flight_id: str = Field(..., description="Catalog flight identifier (e.g. 'FL-TYO-101').")
    carrier: str = Field(..., description="Operating airline name.")
    origin_code: str = Field(..., pattern=r"^[A-Z]{3}$")
    destination_code: str = Field(..., pattern=r"^[A-Z]{3}$")
    cabin_class: CabinClass
    roundtrip_fare_usd: float = Field(..., ge=0)
    total_fare_for_group_usd: float = Field(..., ge=0)
    duration_hours: float = Field(..., gt=0)
    stops: int = Field(..., ge=0, le=3)
    refundable: bool
    baggage_included: str


class FlightSearchOutput(StrictSchema):
    """Validated response payload returned by `search_scheduled_flights`."""

    status: Literal["success"] = "success"
    origin_code: str
    destination_code: str
    travelers: int
    matching_count: int
    cheapest_roundtrip_usd: float | None = None
    flights: list[FlightOption]
    note: str = ""


# ---------------------------------------------------------------------------
# Hotel Search Schemas
# ---------------------------------------------------------------------------
class HotelSearchInput(StrictSchema):
    """Validated input parameters for searching vetted hotels."""

    destination: str = Field(
        ...,
        min_length=2,
        max_length=40,
        description="Destination IATA code (e.g. 'TYO', 'LIS') or city name.",
    )
    nights: int = Field(
        ...,
        ge=1,
        le=30,
        description="Length of stay in nights (1-30).",
    )
    max_nightly_usd: float | None = Field(
        default=None,
        gt=0,
        le=10000,
        description="Optional maximum nightly rate in USD before taxes.",
    )
    tier: AccommodationTier | None = Field(
        default=None,
        description="Optional property tier filter: 'budget', 'boutique', or 'luxury'.",
    )


class HotelOption(StrictSchema):
    """Single vetted accommodation option."""

    hotel_id: str = Field(..., description="Catalog hotel identifier (e.g. 'HT-LIS-201').")
    name: str
    destination_code: str = Field(..., pattern=r"^[A-Z]{3}$")
    neighborhood: str
    tier: AccommodationTier
    nightly_rate_usd: float = Field(..., ge=0)
    stay_subtotal_usd: float = Field(..., ge=0, description="Nightly rate * nights.")
    rating: float = Field(..., ge=0, le=5.0)
    breakfast_included: bool
    free_cancellation: bool
    accessibility_features: str


class HotelSearchOutput(StrictSchema):
    """Validated response payload returned by `search_vetted_hotels`."""

    status: Literal["success"] = "success"
    destination_code: str
    nights: int
    matching_count: int
    lowest_nightly_usd: float | None = None
    hotels: list[HotelOption]
    note: str = ""


# ---------------------------------------------------------------------------
# Destination Advisory Schemas
# ---------------------------------------------------------------------------
class DestinationAdvisoryInput(StrictSchema):
    """Validated input for retrieving destination advisories and daily cost norms."""

    destination: str = Field(
        ...,
        min_length=2,
        max_length=40,
        description="Destination IATA code or city name.",
    )


class DestinationAdvisory(StrictSchema):
    """Safety, entry, climate, and local daily budget advisory for a destination."""

    destination_code: str = Field(..., pattern=r"^[A-Z]{3}$")
    city: str
    country: str
    currency_code: str = Field(..., pattern=r"^[A-Z]{3}$")
    safety_level: int = Field(
        ...,
        ge=1,
        le=4,
        description="1=Exercise Normal Precautions, 2=Increased Caution, 3=Reconsider, 4=Do Not Travel.",
    )
    visa_and_entry_notes: str
    seasonal_weather_summary: str
    avg_daily_meals_and_transit_usd: float = Field(..., ge=0)
    safety_guidance: str
    booking_permitted: bool


class DestinationAdvisoryOutput(StrictSchema):
    """Validated response payload returned by `retrieve_destination_advisory`."""

    status: Literal["success"] = "success"
    advisory: DestinationAdvisory


# ---------------------------------------------------------------------------
# Budget Breakdown Calculator Schemas
# ---------------------------------------------------------------------------
class BudgetBreakdownInput(StrictSchema):
    """Input parameters for deterministic itinerary cost auditing."""

    target_budget_usd: float = Field(
        ...,
        gt=0,
        le=100000,
        description="Traveler's total all-in trip budget ceiling in USD.",
    )
    flight_roundtrip_per_person_usd: float = Field(
        ...,
        ge=0,
        le=25000,
        description="Selected roundtrip flight fare per traveler in USD.",
    )
    hotel_nightly_rate_usd: float = Field(
        ...,
        ge=0,
        le=10000,
        description="Selected nightly hotel rate in USD (total across shared rooms).",
    )
    nights: int = Field(..., ge=1, le=30, description="Number of hotel nights.")
    daily_local_spend_per_person_usd: float = Field(
        ...,
        ge=0,
        le=2000,
        description="Estimated daily meals, transit, and activities spend per person in USD.",
    )
    travelers: int = Field(default=1, ge=1, le=9, description="Total number of travelers.")
    contingency_pct: float = Field(
        default=8.0,
        ge=0.0,
        le=30.0,
        description="Recommended contingency buffer percentage (default 8%).",
    )


class BudgetBreakdownOutput(StrictSchema):
    """Deterministic itemized budget audit returned by `calculate_itinerary_budget_breakdown`."""

    status: Literal["success"] = "success"
    target_budget_usd: float
    flights_total_usd: float
    lodging_with_taxes_usd: float
    local_expenses_total_usd: float
    contingency_buffer_usd: float
    estimated_grand_total_usd: float
    remaining_headroom_usd: float
    within_budget: bool
    utilization_pct: float
    optimization_recommendations: list[str]


# ---------------------------------------------------------------------------
# Traveler Memory / Preference Schemas
# ---------------------------------------------------------------------------
class TravelerPreferencesInput(StrictSchema):
    """Input parameters for loading saved traveler preferences."""

    traveler_id: str = Field(
        default="default_traveler",
        min_length=2,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Safe alphanumeric traveler profile identifier.",
    )


class SavePreferenceInput(StrictSchema):
    """Input parameters for storing a persistent non-PII traveler preference."""

    traveler_id: str = Field(
        default="default_traveler",
        min_length=2,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    category: Literal[
        "seat",
        "dietary",
        "accommodation",
        "airline_loyalty",
        "accessibility",
        "pacing",
        "budget_style",
    ] = Field(..., description="Structured preference category.")
    preference_value: str = Field(
        ...,
        min_length=2,
        max_length=240,
        description="Scrubbed non-PII preference description (e.g. 'Vegetarian meals, aisle seat').",
    )


class TravelerPreferenceRecord(StrictSchema):
    """Single stored long-term preference record."""

    category: str
    preference_value: str
    source: str
    updated_at: str


class TravelerPreferencesOutput(StrictSchema):
    """Validated response payload for preference lookup and updates."""

    status: Literal["success"] = "success"
    traveler_id: str
    preferences: list[TravelerPreferenceRecord]
    message: str = ""


# ---------------------------------------------------------------------------
# Booking & Cancellation Schemas (Human-in-the-Loop Protected)
# ---------------------------------------------------------------------------
class BookingRequestInput(StrictSchema):
    """Input schema for committing an approved travel itinerary reservation."""

    traveler_id: str = Field(
        default="default_traveler",
        min_length=2,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
    )
    flight_id: str = Field(
        ...,
        pattern=r"^FL-[A-Z]{3}-\d{3}$",
        description="Exact catalog flight ID (e.g. 'FL-LIS-101').",
    )
    hotel_id: str = Field(
        ...,
        pattern=r"^HT-[A-Z]{3}-\d{3}$",
        description="Exact catalog hotel ID (e.g. 'HT-LIS-201').",
    )
    departure_date: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="Departure date in ISO-8601 YYYY-MM-DD format.",
    )
    nights: int = Field(..., ge=1, le=30)
    travelers: int = Field(default=1, ge=1, le=9)
    approved_budget_usd: float = Field(..., gt=0, le=25000)
    special_requests: str = Field(
        default="",
        max_length=300,
        description="Optional scrubbed dietary or accessibility notes (no PII).",
    )

    @field_validator("departure_date")
    @classmethod
    def _validate_calendar_date(cls, value: str) -> str:
        from datetime import date

        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("departure_date must be a valid YYYY-MM-DD calendar date.") from exc
        return value


class CancelReservationInput(StrictSchema):
    """Input schema for canceling a previously confirmed booking."""

    reservation_id: str = Field(
        ...,
        pattern=r"^TRV-[A-Z0-9]{8}$",
        description="8-character reservation confirmation code prefixed with 'TRV-'.",
    )
    reason: str = Field(
        ...,
        min_length=3,
        max_length=240,
        description="Brief reason for canceling the reservation.",
    )


class BookingRecord(StrictSchema):
    """Persisted itinerary reservation summary."""

    reservation_id: str
    traveler_id: str
    destination_code: str
    flight_id: str
    hotel_id: str
    departure_date: str
    nights: int
    travelers: int
    total_committed_usd: float
    status: Literal["CONFIRMED", "CANCELLED"]
    created_at: str


class BookingActionOutput(StrictSchema):
    """Validated response payload returned by reservation/cancellation tools."""

    status: Literal["success"] = "success"
    booking: BookingRecord
    confirmation_message: str
