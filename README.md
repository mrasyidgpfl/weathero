# weathero

A small REST API for detecting temperature anomalies at geographic locations,
built as a 300-line script demonstrating clean Python practice.

## What it does

Given a location (lat/lon), fetches historical temperature data from
Open-Meteo, computes a baseline against a reference period (e.g. 1990-2020),
and detects observed days that deviate by more than N standard deviations.

## Quick start

```bash
uv sync
uv run python weathero.py
# In another shell:
curl localhost:8000/locations
```

The server starts on `http://127.0.0.1:8000`. Interactive API docs are at `/docs`.

## Stack

- Python 3.12, FastAPI, Pydantic, httpx, SQLite stdlib
- uv for dependency management
- pytest for tests
 
## Endpoints
 
| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/locations` | Register a new location |
| `GET` | `/locations` | List all locations |
| `POST` | `/locations/{id}/observations/refresh` | Fetch from Open-Meteo and persist |
| `GET` | `/locations/{id}/observations` | List stored observations in a date range |
| `GET` | `/locations/{id}/baseline` | Compute baseline statistics for a period |
| `GET` | `/locations/{id}/anomalies` | Detect anomalies against a baseline |

## Example interaction
 
```bash
# Register Edinburgh as a monitored location
curl -X POST http://localhost:8000/locations \
  -H "Content-Type: application/json" \
  -d '{"name": "Edinburgh", "latitude": 55.9533, "longitude": -3.1883}'
 
# Pull a year of observations from Open-Meteo
curl -X POST "http://localhost:8000/locations/1/observations/refresh\
?start=2010-01-01&end=2010-12-31"
 
# Compute a baseline for that period
curl "http://localhost:8000/locations/1/baseline\
?period_start=2010-01-01&period_end=2010-12-31"
# {"mean_celsius": 7.54, "std_celsius": 5.89, "sample_count": 365, ...}
 
# Detect anomalies in a recent window against that baseline
curl "http://localhost:8000/locations/1/anomalies\
?baseline_start=2010-01-01&baseline_end=2010-12-31\
&observed_start=2024-07-01&observed_end=2024-07-31\
&threshold_sigmas=2.0"
```

## Conventions
 
- WMO reference period 1991–2020 is the recommended production baseline; the example above uses 2010 for a faster smoke test
- All dates are UTC; Open-Meteo is queried with `timezone=UTC`
- Temperatures are daily means in degrees Celsius