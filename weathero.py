import os
import sqlite3

from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

OPEN_METEO_URL = "https://archive-api.open-meteo.com/v1/archive"
HTTP_TIMEOUT_SECONDS = 30.0
DB_PATH = Path(os.getenv("WEATHERO_DB", "weathero.db"))
SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    latitude    REAL NOT NULL,
    longitude   REAL NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class Observation(BaseModel):
    """A single temperature observation at a location on a date."""
    model_config = ConfigDict(from_attributes=True)

    id: int
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
        if "period_start" in info.data and v <= info.data["period_start"]:
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
    conn = sqlite3.connect(
        DB_PATH,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
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

# Weather Data Source
class _OpenMeteoDaily(BaseModel):
    """Internal model matching Open-Meteo's daily response shape."""
    time: list[date]
    temperature_2m_mean: list[float | None]


class _OpenMeteoResponse(BaseModel):
    """Internal model for the full Open-Meteo archive response."""
    latitude: float
    longitude: float
    daily: _OpenMeteoDaily


def fetch_observations(
    location: Location,
    start: date,
    end: date,
) -> list[Observation]:
    """Fetch daily mean temperatures from Open-Meteo for a date range.

    Skips days where the source returned null (data gaps in the archive).
    Raises httpx.HTTPError on network/HTTP failures.
    """
    params = {
        "latitude": location.latitude,
        "longitude": location.longitude,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "daily": "temperature_2m_mean",
        "timezone": "UTC",
    }
    response = httpx.get(
        OPEN_METEO_URL,
        params=params,
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = _OpenMeteoResponse.model_validate(response.json())

    observations: list[Observation] = []
    for obs_date, temp in zip(payload.daily.time, payload.daily.temperature_2m_mean):
        if temp is None:
            continue
        observations.append(
            Observation(
                id=0,
                location_id=location.id,
                observed_on=obs_date,
                temperature_celsius=temp,
            )
        )
    return observations


# Entry point

def main() -> None:
    init_db()
    print(f"weathero ready, DB at {DB_PATH.resolve()}")

    # Smoke test: fetch a week of Edinburgh weather
    edinburgh = Location(
        id=1,
        name="Edinburgh",
        latitude=55.9533,
        longitude=-3.1883,
        created_at=datetime.now(),
    )
    observations = fetch_observations(
        edinburgh,
        date(2024, 1, 1),
        date(2024, 1, 7),
    )
    print(f"Fetched {len(observations)} observations")
    for obs in observations[:3]:
        print(f"  {obs.observed_on}: {obs.temperature_celsius:.1f}°C")


if __name__ == "__main__":
    main()