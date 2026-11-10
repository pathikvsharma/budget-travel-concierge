"""Verification tests for Pillar 1 (Tools & Schemas) and HITL booking tools."""

from __future__ import annotations

from types import SimpleNamespace
import pytest
from pydantic import ValidationError

from travel_concierge.schemas import FlightSearchInput
from travel_concierge.tools import (
    calculate_itinerary_budget_breakdown,
    cancel_confirmed_reservation,
    cancel_confirmed_reservation_tool,
    load_traveler_preferences,
    lookup_reservation_status,
    requires_booking_confirmation,
    reserve_approved_itinerary,
    reserve_approved_itinerary_tool,
    retrieve_destination_advisory,
    save_traveler_preference_note,
    search_scheduled_flights,
    search_vetted_hotels,
)


def test_all_tools_have_descriptive_names_and_rich_docstrings() -> None:
    tool_funcs = [
        search_scheduled_flights,
        search_vetted_hotels,
        retrieve_destination_advisory,
        calculate_itinerary_budget_breakdown,
        load_traveler_preferences,
        save_traveler_preference_note,
        reserve_approved_itinerary,
        cancel_confirmed_reservation,
        lookup_reservation_status,
    ]
    for fn in tool_funcs:
        assert "_" in fn.__name__ and len(fn.__name__) >= 18
        assert fn.__doc__ is not None
        assert "Args:" in fn.__doc__
        assert "Returns:" in fn.__doc__


def test_strict_pydantic_schema_forbids_undeclared_fields() -> None:
    with pytest.raises(ValidationError):
        FlightSearchInput.model_validate(
            {
                "origin": "NYC",
                "destination": "LIS",
                "unexpected_extra_field": "rejected",
            }
        )


def test_flight_and_hotel_catalog_lookups_succeed() -> None:
    flights = search_scheduled_flights(origin="NYC", destination="Lisbon", travelers=2)
    assert flights["status"] == "success"
    assert flights["origin_code"] == "NYC"
    assert flights["destination_code"] == "LIS"
    assert flights["matching_count"] >= 2
    assert flights["flights"][0]["total_fare_for_group_usd"] == 1040.0

    hotels = search_vetted_hotels(destination="LIS", nights=4, tier="budget")
    assert hotels["status"] == "success"
    assert hotels["matching_count"] == 1
    assert hotels["hotels"][0]["hotel_id"] == "HT-LIS-101"
    assert hotels["hotels"][0]["stay_subtotal_usd"] == 352.0


def test_guided_error_handling_returns_actionable_recovery_guidance() -> None:
    bad_city = search_scheduled_flights(origin="NYC", destination="Atlantis")
    assert bad_city["status"] == "error"
    assert bad_city["error_code"] == "UNSUPPORTED_DESTINATION"
    assert "recovery_guidance" in bad_city and len(bad_city["recovery_guidance"]) > 20
    assert "LIS" in bad_city["supported_values"]

    sql_injection_input = search_vetted_hotels(
        destination="LIS'; DROP TABLE hotels; --",
        nights=3,
    )
    assert sql_injection_input["status"] == "error"
    # Verify table remains completely intact after malicious input.
    intact = search_vetted_hotels(destination="LIS", nights=3)
    assert intact["status"] == "success" and intact["matching_count"] == 3


def test_budget_breakdown_identifies_compliant_and_over_budget_plans() -> None:
    under = calculate_itinerary_budget_breakdown(
        target_budget_usd=1600.0,
        flight_roundtrip_per_person_usd=520.0,
        hotel_nightly_rate_usd=88.0,
        nights=4,
        daily_local_spend_per_person_usd=55.0,
        travelers=1,
    )
    assert under["status"] == "success"
    assert under["within_budget"] is True
    assert under["remaining_headroom_usd"] > 300.0

    over = calculate_itinerary_budget_breakdown(
        target_budget_usd=1200.0,
        flight_roundtrip_per_person_usd=2490.0,
        hotel_nightly_rate_usd=590.0,
        nights=5,
        daily_local_spend_per_person_usd=85.0,
        travelers=1,
    )
    assert over["within_budget"] is False
    assert any("exceeds target budget" in rec for rec in over["optimization_recommendations"])


def test_hitl_booking_confirmation_lifecycle_and_user_ownership_isolation() -> None:
    assert reserve_approved_itinerary_tool._require_confirmation is requires_booking_confirmation
    assert cancel_confirmed_reservation_tool._require_confirmation is True
    assert (
        requires_booking_confirmation(
            flight_id="FL-LIS-201",
            hotel_id="HT-LIS-101",
            departure_date="2026-12-10",
            nights=4,
            approved_budget_usd=1100.0,
        )
        is True
    )

    # Declined HITL confirmation must NOT create a reservation.
    declined_ctx = SimpleNamespace(
        user_id="alice",
        state={},
        tool_confirmation=SimpleNamespace(confirmed=False),
    )
    declined = reserve_approved_itinerary(
        flight_id="FL-LIS-201",
        hotel_id="HT-LIS-101",
        departure_date="2026-12-10",
        nights=4,
        approved_budget_usd=1100.0,
        tool_context=declined_ctx,  # type: ignore[arg-type]
    )
    assert declined["status"] == "error"
    assert declined["error_code"] == "HUMAN_CONFIRMATION_DECLINED"

    # Approved HITL confirmation creates reservation scoped to user 'alice'.
    approved_ctx = SimpleNamespace(
        user_id="alice",
        state={},
        tool_confirmation=SimpleNamespace(confirmed=True),
    )
    booked = reserve_approved_itinerary(
        flight_id="FL-LIS-201",
        hotel_id="HT-LIS-101",
        departure_date="2026-12-10",
        nights=4,
        approved_budget_usd=1100.0,
        special_requests="Vegetarian meal; contact alice@example.com",
        tool_context=approved_ctx,  # type: ignore[arg-type]
    )
    assert booked["status"] == "success"
    res_id = booked["booking"]["reservation_id"]
    assert res_id.startswith("TRV-")

    # Another user ('bob') MUST NOT read or cancel Alice's booking (BOLA check).
    bob_ctx = SimpleNamespace(
        user_id="bob",
        state={},
        tool_confirmation=SimpleNamespace(confirmed=True),
    )
    unauthorized = lookup_reservation_status(res_id, tool_context=bob_ctx)  # type: ignore[arg-type]
    assert unauthorized["status"] == "error"
    assert unauthorized["error_code"] == "RESERVATION_NOT_FOUND"

    # Alice can look up and cancel her own booking.
    cancelled = cancel_confirmed_reservation(
        reservation_id=res_id,
        reason="Rescheduling trip dates",
        tool_context=approved_ctx,  # type: ignore[arg-type]
    )
    assert cancelled["status"] == "success"
    assert cancelled["booking"]["status"] == "CANCELLED"


def test_preference_tools_scrub_pii_and_persist() -> None:
    ctx = SimpleNamespace(user_id="traveler_pref_1", state={})
    saved = save_traveler_preference_note(
        category="dietary",
        preference_value="Vegan meals; email me at secret@example.com",
        tool_context=ctx,  # type: ignore[arg-type]
    )
    assert saved["status"] == "success"
    assert "[REDACTED_EMAIL]" in saved["preferences"][0]["preference_value"]
    assert "secret@example.com" not in saved["preferences"][0]["preference_value"]

    loaded = load_traveler_preferences(tool_context=ctx)  # type: ignore[arg-type]
    assert loaded["preferences"][0]["category"] == "dietary"
