import os
import sqlite3
import statistics
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Query
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, field_validator

OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"
HTTP_TIMEOUT_SECONDS = 30.0
DB_PATH = Path(os.getenv("WEATHERO_DB", "weathero.db"))

sqlite3.register_adapter(date, lambda d: d.isoformat())
sqlite3.register_converter("DATE", lambda s: date.fromisoformat(s.decode()))

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    created_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS observations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id         INTEGER NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
    observed_on         DATE NOT NULL,
    temperature_celsius REAL NOT NULL,
    UNIQUE(location_id, observed_on)
);
CREATE INDEX IF NOT EXISTS idx_observations_location_date
    ON observations(location_id, observed_on);
"""


# Domain models

class LocationCreate(BaseModel):
    """Input shape for creating a new monitored location."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=100)
    latitude: float = Field(..., ge=-90, le=90)
    longitude: float = Field(..., ge=-180, le=180)


class Location(LocationCreate):
    """A monitored location with persistence metadata."""
    id: int
    created_at: datetime


class Observation(BaseModel):
    """A single temperature observation at a location on a date."""
    id: int | None = None
    location_id: int
    observed_on: date
    temperature_celsius: float = Field(..., ge=-100, le=70)


class Baseline(BaseModel):
    """Statistical baseline for a location over a reference period."""
    location_id: int
    period_start: date
    period_end: date
    mean_celsius: float
    std_celsius: float = Field(..., ge=0)
    sample_count: int = Field(..., gt=0)

    @field_validator("period_end")
    @classmethod
    def end_after_start(cls, v: date, info) -> date:
        start = info.data.get("period_start")
        if start and v <= start:
            raise ValueError("period_end must be after period_start")
        return v


class Anomaly(BaseModel):
    """A deviation flagged against a baseline."""
    location_id: int
    observed_on: date
    observed_celsius: float
    expected_celsius: float
    deviation_sigmas: float

    @property
    def direction(self) -> str:
        return "warm" if self.deviation_sigmas > 0 else "cold"


# Persistence

@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """Yield a SQLite connection with sensible defaults and proper cleanup."""
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """Create tables if they don't exist. Idempotent."""
    with get_connection() as conn:
        conn.executescript(SCHEMA)


# Weather data source

class _OpenMeteoDaily(BaseModel):
    time: list[date]
    temperature_2m_mean: list[float | None]


class _OpenMeteoResponse(BaseModel):
    latitude: float
    longitude: float
    daily: _OpenMeteoDaily


def fetch_observations(location: Location, start: date, end: date) -> list[Observation]:
    """Fetch daily mean temperatures from Open-Meteo, skipping data-gap days."""
    params = {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_mean",
        "timezone": "UTC",
    }
    response = httpx.get(OPEN_METEO_URL, params=params, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    payload = _OpenMeteoResponse.model_validate(response.json())
    return [
        Observation(location_id=location.id, observed_on=d, temperature_celsius=t)
        for d, t in zip(payload.daily.time, payload.daily.temperature_2m_mean)
        if t is not None
    ]


# Analysis

def save_observations(observations: list[Observation]) -> int:
    """Persist observations, skipping duplicates on (location, date). Returns insert count."""
    if not observations:
        return 0
    rows = [(o.location_id, o.observed_on, o.temperature_celsius) for o in observations]
    with get_connection() as conn:
        cursor = conn.executemany(
            "INSERT OR IGNORE INTO observations (location_id, observed_on, temperature_celsius) "
            "VALUES (?, ?, ?)",
            rows,
        )
        return cursor.rowcount


def get_observations(location_id: int, start: date, end: date) -> list[Observation]:
    """Fetch stored observations for a location within an inclusive date range."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, location_id, observed_on, temperature_celsius FROM observations "
            "WHERE location_id = ? AND observed_on BETWEEN ? AND ? ORDER BY observed_on",
            (location_id, start, end),
        ).fetchall()
    return [Observation.model_validate(dict(row)) for row in rows]


def compute_baseline(location_id: int, period_start: date, period_end: date) -> Baseline:
    """Compute mean and sample stdev over a reference period. Needs >= 2 observations."""
    obs = get_observations(location_id, period_start, period_end)
    if len(obs) < 2:
        raise ValueError(f"Need at least 2 observations, got {len(obs)}")
    temps = [o.temperature_celsius for o in obs]
    return Baseline(
        location_id=location_id, period_start=period_start, period_end=period_end,
        mean_celsius=statistics.mean(temps), std_celsius=statistics.stdev(temps),
        sample_count=len(temps),
    )


def detect_anomalies(
    observations: list[Observation],
    baseline: Baseline,
    threshold_sigmas: float = 2.0,
) -> list[Anomaly]:
    """Flag observations deviating from baseline by more than threshold_sigmas."""
    if baseline.std_celsius == 0:
        return []
    anomalies = []
    for obs in observations:
        dev = (obs.temperature_celsius - baseline.mean_celsius) / baseline.std_celsius
        if abs(dev) >= threshold_sigmas:
            anomalies.append(Anomaly(
                location_id=obs.location_id, observed_on=obs.observed_on,
                observed_celsius=obs.temperature_celsius,
                expected_celsius=baseline.mean_celsius, deviation_sigmas=dev,
            ))
    return anomalies


# API

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="weathero",
    description="Detect temperature anomalies against a historical baseline.",
    version="0.1.0",
    lifespan=lifespan,
)

LOCATION_COLUMNS = "id, name, latitude, longitude, created_at"


def _get_location_or_404(location_id: int) -> Location:
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT {LOCATION_COLUMNS} FROM locations WHERE id = ?", (location_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"Location {location_id} not found")
    return Location.model_validate(dict(row))


@app.post("/locations", response_model=Location, status_code=201)
def create_location(payload: LocationCreate) -> Location:
    """Register a new location for monitoring."""
    with get_connection() as conn:
        try:
            cursor = conn.execute(
                "INSERT INTO locations (name, latitude, longitude) VALUES (?, ?, ?)",
                (payload.name, payload.latitude, payload.longitude),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, f"Location '{payload.name}' already exists")
        row = conn.execute(
            f"SELECT {LOCATION_COLUMNS} FROM locations WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return Location.model_validate(dict(row))


@app.get("/locations", response_model=list[Location])
def list_locations() -> list[Location]:
    """List all monitored locations."""
    with get_connection() as conn:
        rows = conn.execute(f"SELECT {LOCATION_COLUMNS} FROM locations ORDER BY id").fetchall()
    return [Location.model_validate(dict(row)) for row in rows]


@app.post("/locations/{location_id}/observations/refresh")
def refresh_observations(
    location_id: int,
    start: date = Query(..., description="Start date (inclusive)"),
    end: date = Query(..., description="End date (inclusive)"),
) -> dict:
    """Fetch observations from Open-Meteo for a date range and persist them."""
    if end <= start:
        raise HTTPException(400, "end must be after start")
    location = _get_location_or_404(location_id)
    try:
        obs = fetch_observations(location, start, end)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"Upstream weather API error: {exc}") from exc
    return {"location_id": location_id, "fetched": len(obs), "inserted": save_observations(obs)}


@app.get("/locations/{location_id}/observations", response_model=list[Observation])
def list_observations(location_id: int, start: date = Query(...), end: date = Query(...)) -> list[Observation]:
    """Return stored observations for a location within a date range (inclusive)."""
    if end < start:
        raise HTTPException(400, "end must be on or after start")
    _get_location_or_404(location_id)
    return get_observations(location_id, start, end)


@app.get("/locations/{location_id}/baseline", response_model=Baseline)
def get_baseline(
    location_id: int, period_start: date = Query(...), period_end: date = Query(...),
) -> Baseline:
    """Compute a statistical baseline for a location over a reference period."""
    _get_location_or_404(location_id)
    try:
        return compute_baseline(location_id, period_start, period_end)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/locations/{location_id}/anomalies", response_model=list[Anomaly])
def list_anomalies(
    location_id: int,
    baseline_start: date = Query(..., description="Baseline period start"),
    baseline_end: date = Query(..., description="Baseline period end"),
    observed_start: date = Query(..., description="Observation window start"),
    observed_end: date = Query(..., description="Observation window end"),
    threshold_sigmas: float = Query(2.0, ge=0.5, le=5.0, description="Anomaly threshold"),
) -> list[Anomaly]:
    """Detect anomalies in an observation window relative to a baseline."""
    _get_location_or_404(location_id)
    try:
        baseline = compute_baseline(location_id, baseline_start, baseline_end)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    obs = get_observations(location_id, observed_start, observed_end)
    return detect_anomalies(obs, baseline, threshold_sigmas)


# Tests

@pytest.fixture
def client(tmp_path, monkeypatch):
    """FastAPI TestClient with an isolated SQLite DB per test."""
    monkeypatch.setattr("weathero.DB_PATH", tmp_path / "test.db")
    init_db()
    return TestClient(app)


def test_anomaly_detection_math() -> None:
    """detect_anomalies computes z-scores correctly and respects threshold."""
    baseline = Baseline(
        location_id=1, period_start=date(2010, 1, 1), period_end=date(2010, 12, 31),
        mean_celsius=10.0, std_celsius=2.0, sample_count=365,
    )
    obs = [
        Observation(location_id=1, observed_on=date(2024, 1, d), temperature_celsius=t)
        for d, t in [(1, 10.0), (2, 15.0), (3, 4.0), (4, 13.0)]
    ]
    anomalies = detect_anomalies(obs, baseline, threshold_sigmas=2.0)
    assert len(anomalies) == 2
    assert anomalies[0].deviation_sigmas == pytest.approx(2.5)
    assert anomalies[0].direction == "warm"
    assert anomalies[1].deviation_sigmas == pytest.approx(-3.0)
    assert anomalies[1].direction == "cold"


def test_baseline_rejects_inverted_period() -> None:
    """Baseline model rejects period_end <= period_start."""
    with pytest.raises(ValueError, match="period_end must be after period_start"):
        Baseline(
            location_id=1, period_start=date(2020, 1, 1), period_end=date(2010, 1, 1),
            mean_celsius=10.0, std_celsius=2.0, sample_count=100,
        )


def test_api_end_to_end(client) -> None:
    """Create a location, seed observations, compute baseline via API."""
    response = client.post("/locations", json={"name": "TestLoc", "latitude": 0.0, "longitude": 0.0})
    assert response.status_code == 201
    location_id = response.json()["id"]
    save_observations([
        Observation(location_id=location_id, observed_on=date(2010, 1, d), temperature_celsius=10.0 + d * 0.1)
        for d in range(1, 31)
    ])
    response = client.get(
        f"/locations/{location_id}/baseline",
        params={"period_start": "2010-01-01", "period_end": "2010-01-30"},
    )
    assert response.status_code == 200
    baseline = response.json()
    assert baseline["sample_count"] == 30
    assert baseline["mean_celsius"] == pytest.approx(11.55, abs=0.01)


# Entry point

def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()