from datetime import date, datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator


class LocationCreate(BaseModel):
    """Input shape for creating a new monitored location."""
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

def main():
    print("Hello from weathero!")


if __name__ == "__main__":
    main()
