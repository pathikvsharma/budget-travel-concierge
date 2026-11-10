"""Parameterized SQLite persistence for travel catalog, bookings, and long-term memory.

Security properties:
- 100% parameterized SQL statements (`?` bindings); zero string interpolation.
- Strict directory boundary checks via `config.load_settings()`.
- Async preference consolidation using `aiosqlite` so memory writes never block.
- TODO(security): When migrating from embedded single-tenant SQLite to multi-tenant
  Cloud SQL PostgreSQL in production, enforce Cloud SQL IAM database authentication,
  mTLS (`sslmode=verify-full`), and separate read-only (`catalog_reader`) vs.
  booking (`reservation_writer`) database roles following least privilege.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import secrets
import sqlite3
import threading
from typing import Any, Iterator

import aiosqlite

from travel_concierge.config import load_settings
from travel_concierge.pii import redact_pii_text

_INIT_LOCK = threading.Lock()
_INITIALIZED_PATHS: set[Path] = set()

# City alias allow-list mapping normalized names/IATA codes to canonical 3-letter codes.
CITY_TO_IATA: dict[str, str] = {
    "TYO": "TYO",
    "NRT": "TYO",
    "HND": "TYO",
    "TOKYO": "TYO",
    "LIS": "LIS",
    "LISBON": "LIS",
    "PAR": "PAR",
    "CDG": "PAR",
    "PARIS": "PAR",
    "MEX": "MEX",
    "MEXICO CITY": "MEX",
    "CDMX": "MEX",
    "LON": "LON",
    "LHR": "LON",
    "LONDON": "LON",
    "NYC": "NYC",
    "JFK": "NYC",
    "EWR": "NYC",
    "NEW YORK": "NYC",
    "SFO": "SFO",
    "SAN FRANCISCO": "SFO",
    "LAX": "LAX",
    "LOS ANGELES": "LAX",
    "KBL": "KBL",
    "KABUL": "KBL",
}

SUPPORTED_BOOKABLE_DESTINATIONS: tuple[str, ...] = (
    "LIS",
    "LON",
    "MEX",
    "NYC",
    "PAR",
    "SFO",
    "TYO",
)

_SEED_ADVISORIES: tuple[tuple[Any, ...], ...] = (
    (
        "TYO",
        "Tokyo",
        "Japan",
        "JPY",
        1,
        "Visa-free up to 90 days for many tourists (verify Visit Japan Web QR prior to arrival).",
        "Mild springs/autumns (14-22C), warm humid summers, crisp dry winters.",
        65.0,
        "Level 1 — Exercise normal precautions. Keep transit IC card handy and observe earthquake signage.",
        1,
    ),
    (
        "LIS",
        "Lisbon",
        "Portugal",
        "EUR",
        1,
        "Schengen 90/180-day short-stay rule applies; passport must have 3+ months validity past departure.",
        "Sunny Mediterranean climate (15-28C most of the year); steep cobblestone hills.",
        55.0,
        "Level 1 — Exercise normal precautions; guard belongings on historic Tram 28.",
        1,
    ),
    (
        "PAR",
        "Paris",
        "France",
        "EUR",
        2,
        "Schengen short-stay area; verify ETIAS applicability and 3+ months post-trip passport validity.",
        "Temperate seasons (5-26C); pack light rain protection year-round.",
        85.0,
        "Level 2 — Exercise increased caution around crowded transit hubs and major monuments.",
        1,
    ),
    (
        "MEX",
        "Mexico City",
        "Mexico",
        "MXN",
        2,
        "Valid passport required; confirm FMM entry stamp duration at immigration.",
        "High-altitude subtropical highland (2,240m; 12-25C); afternoon summer rain showers.",
        48.0,
        "Level 2 — Use app-based or authorized site taxis, stick to vetted neighborhoods, and stay hydrated at altitude.",
        1,
    ),
    (
        "LON",
        "London",
        "United Kingdom",
        "GBP",
        1,
        "UK Electronic Travel Authorisation (ETA) required for most visa-exempt travelers prior to boarding.",
        "Cool maritime climate (6-23C) with frequent light showers; contactless transit via Oyster/card.",
        90.0,
        "Level 1 — Exercise normal urban precautions; verify rail schedules before intercity day trips.",
        1,
    ),
    (
        "NYC",
        "New York",
        "United States",
        "USD",
        1,
        "REAL ID / valid passport for domestic flyers; ESTA or US visa required for eligible international arrivals.",
        "Four distinct seasons (-2C winter to 30C humid summer); extensive 24/7 OMNY subway transit.",
        95.0,
        "Level 1 — Standard major-city awareness; stay alert on late-night transit platforms.",
        1,
    ),
    (
        "SFO",
        "San Francisco",
        "United States",
        "USD",
        1,
        "REAL ID / valid passport for domestic flyers; ESTA or US visa for international visitors.",
        "Cool coastal microclimates (10-20C year-round with coastal fog); pack layers.",
        90.0,
        "Level 1 — Never leave luggage visible inside parked rental vehicles.",
        1,
    ),
    (
        "KBL",
        "Kabul",
        "Afghanistan",
        "AFN",
        4,
        "Consular services suspended; commercial entry heavily restricted.",
        "Arid continental climate with cold winters and hot dry summers.",
        60.0,
        "Level 4 — DO NOT TRAVEL due to armed conflict, civil unrest, and kidnapping risk. Booking blocked by TripPilot safety policy.",
        0,
    ),
)

_SEED_FLIGHTS: tuple[tuple[Any, ...], ...] = (
    # From SFO
    ("FL-TYO-101", "PacificWings", "SFO", "TYO", "economy", 780.0, 11.2, 0, 0, "1 carry-on + 1 checked bag"),
    ("FL-TYO-102", "NipponSky", "SFO", "TYO", "premium_economy", 1290.0, 11.0, 0, 1, "2 checked bags"),
    ("FL-TYO-103", "NipponSky", "SFO", "TYO", "business", 3150.0, 11.0, 0, 1, "2 checked bags + lounge"),
    ("FL-LIS-101", "AtlanticAir", "SFO", "LIS", "economy", 610.0, 13.5, 1, 0, "1 carry-on + 1 checked bag"),
    ("FL-LIS-102", "LusitaniaJet", "SFO", "LIS", "premium_economy", 980.0, 11.4, 0, 1, "2 checked bags"),
    ("FL-LIS-103", "LusitaniaJet", "SFO", "LIS", "business", 2420.0, 11.4, 0, 1, "2 checked bags + lounge"),
    ("FL-PAR-101", "EuroBridge", "SFO", "PAR", "economy", 690.0, 11.1, 0, 0, "1 carry-on"),
    ("FL-PAR-102", "AirLumiere", "SFO", "PAR", "premium_economy", 1180.0, 10.9, 0, 1, "2 checked bags"),
    ("FL-PAR-103", "AirLumiere", "SFO", "PAR", "business", 2890.0, 10.9, 0, 1, "2 checked bags + lounge"),
    ("FL-MEX-101", "AztecaAir", "SFO", "MEX", "economy", 340.0, 4.4, 0, 0, "1 carry-on"),
    ("FL-MEX-102", "AztecaAir", "SFO", "MEX", "premium_economy", 540.0, 4.4, 0, 1, "1 checked bag"),
    ("FL-MEX-103", "AztecaAir", "SFO", "MEX", "business", 1120.0, 4.4, 0, 1, "2 checked bags + lounge"),
    ("FL-LON-101", "ThamesAir", "SFO", "LON", "economy", 650.0, 10.4, 0, 0, "1 carry-on + 1 checked bag"),
    ("FL-LON-102", "ThamesAir", "SFO", "LON", "premium_economy", 1140.0, 10.4, 0, 1, "2 checked bags"),
    ("FL-NYC-101", "CoastLine", "SFO", "NYC", "economy", 290.0, 5.5, 0, 0, "1 carry-on"),
    ("FL-NYC-102", "CoastLine", "SFO", "NYC", "business", 980.0, 5.4, 0, 1, "2 checked bags + lie-flat"),
    # From NYC
    ("FL-LIS-201", "LusitaniaJet", "NYC", "LIS", "economy", 520.0, 7.1, 0, 0, "1 carry-on + 1 checked bag"),
    ("FL-LIS-202", "LusitaniaJet", "NYC", "LIS", "premium_economy", 820.0, 7.1, 0, 1, "2 checked bags"),
    ("FL-TYO-201", "NipponSky", "NYC", "TYO", "economy", 890.0, 14.2, 0, 0, "1 carry-on + 1 checked bag"),
    ("FL-TYO-202", "NipponSky", "NYC", "TYO", "premium_economy", 1420.0, 14.2, 0, 1, "2 checked bags"),
    ("FL-PAR-201", "AirLumiere", "NYC", "PAR", "economy", 580.0, 7.5, 0, 0, "1 carry-on + 1 checked bag"),
    ("FL-PAR-202", "AirLumiere", "NYC", "PAR", "business", 2490.0, 7.5, 0, 1, "2 checked bags + lounge"),
    ("FL-LON-201", "ThamesAir", "NYC", "LON", "economy", 540.0, 7.0, 0, 0, "1 carry-on + 1 checked bag"),
    ("FL-LON-202", "ThamesAir", "NYC", "LON", "premium_economy", 920.0, 7.0, 0, 1, "2 checked bags"),
    ("FL-MEX-201", "AztecaAir", "NYC", "MEX", "economy", 370.0, 5.1, 0, 0, "1 carry-on"),
    ("FL-SFO-201", "CoastLine", "NYC", "SFO", "economy", 280.0, 6.1, 0, 0, "1 carry-on"),
    # From LAX
    ("FL-TYO-301", "PacificWings", "LAX", "TYO", "economy", 740.0, 11.6, 0, 0, "1 carry-on + 1 checked bag"),
    ("FL-MEX-301", "AztecaAir", "LAX", "MEX", "economy", 310.0, 3.8, 0, 0, "1 carry-on"),
    ("FL-LIS-301", "AtlanticAir", "LAX", "LIS", "economy", 640.0, 13.8, 1, 0, "1 carry-on + 1 checked bag"),
)

_SEED_HOTELS: tuple[tuple[Any, ...], ...] = (
    ("HT-TYO-101", "Ueno Smart Business Inn", "TYO", "Ueno", "budget", 95.0, 4.3, 1, 1, "Step-free lobby and elevator"),
    ("HT-TYO-201", "Shinjuku Garden Boutique", "TYO", "Shinjuku", "boutique", 185.0, 4.7, 1, 1, "Roll-in shower rooms available"),
    ("HT-TYO-301", "Ginza Imperial Suites", "TYO", "Ginza", "luxury", 460.0, 4.9, 1, 1, "Full ADA-equivalent suite access"),
    ("HT-LIS-101", "Baixa Sunny Guesthouse", "LIS", "Baixa", "budget", 88.0, 4.4, 1, 1, "Ground-floor accessible rooms"),
    ("HT-LIS-201", "Chiado Azulejo Boutique", "LIS", "Chiado", "boutique", 165.0, 4.8, 1, 1, "Elevator access to all floors"),
    ("HT-LIS-301", "Avenida Liberdade Palace", "LIS", "Avenida", "luxury", 340.0, 4.9, 1, 1, "Full mobility & visual accessibility"),
    ("HT-PAR-101", "Latin Quarter Metro Inn", "PAR", "5th Arr.", "budget", 125.0, 4.2, 0, 1, "Compact lift, ground-floor accessible room"),
    ("HT-PAR-201", "Hotel du Parc Marais", "PAR", "Le Marais", "boutique", 230.0, 4.7, 1, 1, "Step-free entrance and accessible bath"),
    ("HT-PAR-301", "Maison Vendome Prestige", "PAR", "1st Arr.", "luxury", 590.0, 4.9, 1, 1, "Full concierge mobility support"),
    ("HT-MEX-101", "Condesa Verde Posada", "MEX", "La Condesa", "budget", 72.0, 4.5, 1, 1, "Courtyard ground-floor rooms"),
    ("HT-MEX-201", "Casa Roma Norte Boutique", "MEX", "Roma Norte", "boutique", 145.0, 4.8, 1, 1, "Elevator and step-free entrance"),
    ("HT-MEX-301", "Polanco Grand Reforma", "MEX", "Polanco", "luxury", 310.0, 4.9, 1, 1, "Full accessibility suites"),
    ("HT-LON-101", "Bloomsbury Park Compact", "LON", "Bloomsbury", "budget", 135.0, 4.3, 0, 1, "Accessible lift and wet-room option"),
    ("HT-LON-201", "Covent Garden Townhouse", "LON", "West End", "boutique", 255.0, 4.7, 1, 1, "Step-free access via concierge ramp"),
    ("HT-LON-301", "Mayfair Savoy Manor", "LON", "Mayfair", "luxury", 540.0, 4.9, 1, 1, "Full accessible luxury rooms"),
    ("HT-NYC-101", "Midtown Transit Pod Hotel", "NYC", "Midtown", "budget", 145.0, 4.2, 0, 1, "ADA compliant elevator and rooms"),
    ("HT-NYC-201", "SoHo Cast-Iron Boutique", "NYC", "SoHo", "boutique", 275.0, 4.7, 1, 1, "ADA accessible studio suites"),
    ("HT-NYC-301", "Fifth Avenue Park Tower", "NYC", "Central Park", "luxury", 580.0, 4.9, 1, 1, "Full ADA accessibility"),
    ("HT-SFO-101", "Marina Bay Breeze Inn", "SFO", "Marina", "budget", 140.0, 4.3, 1, 1, "ADA ground-floor rooms"),
    ("HT-SFO-201", "Nob Hill Cable Boutique", "SFO", "Nob Hill", "boutique", 235.0, 4.6, 1, 1, "ADA elevator and roll-in shower"),
    ("HT-SFO-301", "Embarcadero Waterfront Grand", "SFO", "Waterfront", "luxury", 490.0, 4.9, 1, 1, "Full ADA waterfront suites"),
)


def normalize_location_code(raw_location: str) -> str | None:
    """Resolve an IATA code or city name against the strict canonical allow-list."""
    cleaned = " ".join(raw_location.strip().upper().split())
    if cleaned in CITY_TO_IATA:
        return CITY_TO_IATA[cleaned]
    # Handle inputs such as "Lisbon, Portugal" or "Tokyo (TYO)"
    city_part = cleaned.split(",")[0].split("(")[0].strip()
    return CITY_TO_IATA.get(city_part)


def get_catalog_db_path() -> Path:
    """Return the validated catalog SQLite database path and ensure schema seed."""
    db_path = load_settings().catalog_db_path
    ensure_catalog_initialized(db_path)
    return db_path


def ensure_catalog_initialized(db_path: Path) -> None:
    """Create catalog, reservation, and memory tables and seed curated listings."""
    resolved = db_path.resolve()
    if resolved in _INITIALIZED_PATHS and resolved.exists():
        return

    with _INIT_LOCK:
        if resolved in _INITIALIZED_PATHS and resolved.exists():
            return
        resolved.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(resolved, timeout=10.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys=ON;")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS advisories (
                    destination_code TEXT PRIMARY KEY,
                    city TEXT NOT NULL,
                    country TEXT NOT NULL,
                    currency_code TEXT NOT NULL,
                    safety_level INTEGER NOT NULL,
                    visa_and_entry_notes TEXT NOT NULL,
                    seasonal_weather_summary TEXT NOT NULL,
                    avg_daily_meals_and_transit_usd REAL NOT NULL,
                    safety_guidance TEXT NOT NULL,
                    booking_permitted INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS flights (
                    flight_id TEXT PRIMARY KEY,
                    carrier TEXT NOT NULL,
                    origin_code TEXT NOT NULL,
                    destination_code TEXT NOT NULL,
                    cabin_class TEXT NOT NULL,
                    roundtrip_fare_usd REAL NOT NULL,
                    duration_hours REAL NOT NULL,
                    stops INTEGER NOT NULL,
                    refundable INTEGER NOT NULL,
                    baggage_included TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS hotels (
                    hotel_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    destination_code TEXT NOT NULL,
                    neighborhood TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    nightly_rate_usd REAL NOT NULL,
                    rating REAL NOT NULL,
                    breakfast_included INTEGER NOT NULL,
                    free_cancellation INTEGER NOT NULL,
                    accessibility_features TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS traveler_preferences (
                    traveler_id TEXT NOT NULL,
                    category TEXT NOT NULL,
                    preference_value TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (traveler_id, category)
                );

                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY,
                    traveler_id TEXT NOT NULL,
                    destination_code TEXT NOT NULL,
                    flight_id TEXT NOT NULL,
                    hotel_id TEXT NOT NULL,
                    departure_date TEXT NOT NULL,
                    nights INTEGER NOT NULL,
                    travelers INTEGER NOT NULL,
                    total_committed_usd REAL NOT NULL,
                    special_requests TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cancellation_reason TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.executemany(
                """
                INSERT OR IGNORE INTO advisories (
                    destination_code, city, country, currency_code, safety_level,
                    visa_and_entry_notes, seasonal_weather_summary,
                    avg_daily_meals_and_transit_usd, safety_guidance, booking_permitted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _SEED_ADVISORIES,
            )
            conn.executemany(
                """
                INSERT OR IGNORE INTO flights (
                    flight_id, carrier, origin_code, destination_code, cabin_class,
                    roundtrip_fare_usd, duration_hours, stops, refundable, baggage_included
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _SEED_FLIGHTS,
            )
            conn.executemany(
                """
                INSERT OR IGNORE INTO hotels (
                    hotel_id, name, destination_code, neighborhood, tier,
                    nightly_rate_usd, rating, breakfast_included,
                    free_cancellation, accessibility_features
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _SEED_HOTELS,
            )
            conn.commit()
            _INITIALIZED_PATHS.add(resolved)
        finally:
            conn.close()


@contextmanager
def get_db_connection() -> Iterator[sqlite3.Connection]:
    """Yield a row-mapped SQLite connection with foreign keys enforced."""
    db_path = get_catalog_db_path()
    conn = sqlite3.connect(db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def generate_reservation_id() -> str:
    """Generate a cryptographically random confirmation identifier (`TRV-XXXXXXXX`)."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    token = "".join(secrets.choice(alphabet) for _ in range(8))
    return f"TRV-{token}"


def upsert_preference_sync(
    traveler_id: str,
    category: str,
    preference_value: str,
    source: str = "explicit_tool",
) -> dict[str, str]:
    """Store a PII-scrubbed preference synchronously using parameterized SQL."""
    scrubbed_val, _ = redact_pii_text(preference_value)
    now_iso = datetime.now(timezone.utc).isoformat()
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO traveler_preferences (
                traveler_id, category, preference_value, source, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(traveler_id, category) DO UPDATE SET
                preference_value = excluded.preference_value,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (traveler_id, category, scrubbed_val, source, now_iso),
        )
    return {
        "category": category,
        "preference_value": scrubbed_val,
        "source": source,
        "updated_at": now_iso,
    }


def fetch_preferences_sync(traveler_id: str) -> list[dict[str, str]]:
    """Fetch all stored preferences for `traveler_id`."""
    with get_db_connection() as conn:
        rows = conn.execute(
            """
            SELECT category, preference_value, source, updated_at
            FROM traveler_preferences
            WHERE traveler_id = ?
            ORDER BY category ASC
            """,
            (traveler_id,),
        ).fetchall()
    return [dict(row) for row in rows]


async def upsert_preferences_async(
    traveler_id: str,
    extracted_preferences: dict[str, str],
    source: str = "async_memory_callback",
) -> int:
    """Persist consolidated preferences asynchronously via `aiosqlite`."""
    if not extracted_preferences:
        return 0
    db_path = get_catalog_db_path()
    now_iso = datetime.now(timezone.utc).isoformat()
    records: list[tuple[str, str, str, str, str]] = []
    for category, raw_val in extracted_preferences.items():
        scrubbed_val, _ = redact_pii_text(raw_val)
        if scrubbed_val:
            records.append((traveler_id, category, scrubbed_val[:240], source, now_iso))

    if not records:
        return 0

    async with aiosqlite.connect(db_path, timeout=10.0) as db:
        await db.executemany(
            """
            INSERT INTO traveler_preferences (
                traveler_id, category, preference_value, source, updated_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(traveler_id, category) DO UPDATE SET
                preference_value = excluded.preference_value,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            records,
        )
        await db.commit()
    return len(records)


async def fetch_preferences_async(traveler_id: str) -> list[dict[str, str]]:
    """Fetch stored traveler preferences asynchronously via `aiosqlite`."""
    db_path = get_catalog_db_path()
    async with aiosqlite.connect(db_path, timeout=10.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT category, preference_value, source, updated_at
            FROM traveler_preferences
            WHERE traveler_id = ?
            ORDER BY category ASC
            """,
            (traveler_id,),
        )
        rows = await cursor.fetchall()
    return [dict(row) for row in rows]
