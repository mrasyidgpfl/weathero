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

## Stack

- Python 3.12, FastAPI, Pydantic, httpx, SQLite stdlib
- uv for dependency management
- pytest for tests