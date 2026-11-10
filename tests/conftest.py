"""Shared pytest fixtures providing isolated temporary SQLite directories."""

from __future__ import annotations

from pathlib import Path
import pytest

from travel_concierge.observability import RING_SPAN_EXPORTER


@pytest.fixture(autouse=True)
def isolated_travel_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate SQLite catalog, booking ledger, and session DB per test case."""
    data_dir = tmp_path / "trippilot_test_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    session_db = data_dir / "sessions.db"

    monkeypatch.setenv("TRAVEL_DATA_DIR", str(data_dir))
    monkeypatch.setenv(
        "TRAVEL_SESSION_DB_URL",
        f"sqlite+aiosqlite:///{session_db.as_posix()}",
    )
    monkeypatch.setenv("ENABLE_CLOUD_TRACE", "FALSE")
    RING_SPAN_EXPORTER.clear()
    return data_dir
