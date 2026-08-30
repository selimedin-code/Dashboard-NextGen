"""FRED (St. Louis Fed) client — latest observation per series, for the macro
gauges. Free API key from https://fred.stlouisfed.org/docs/api/api_key.html;
set FRED_API_KEY in .env locally and in Render's environment.

Series arrive at different cadences (daily spreads, weekly STLFSI), so each
value is returned with its own observation date and the caller decides how
much staleness to tolerate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

import httpx

BASE = "https://api.stlouisfed.org/fred/series/observations"


@dataclass
class FredValue:
    series_id: str
    value: Decimal
    as_of: date


class FredClient:
    def __init__(self, api_key: str, *, timeout: float = 15.0):
        if not api_key:
            raise RuntimeError("FRED_API_KEY is not set.")
        self._key = api_key
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FredClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def latest(self, series_id: str) -> FredValue:
        """Most recent non-missing observation ('.' rows are skipped)."""
        r = self._client.get(BASE, params={
            "series_id": series_id,
            "api_key": self._key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": 10,          # a few rows so a trailing '.' never blanks us
        })
        r.raise_for_status()
        obs = r.json().get("observations", [])
        for o in obs:
            raw = o.get("value")
            if raw in (None, "", "."):
                continue
            try:
                return FredValue(
                    series_id=series_id,
                    value=Decimal(str(raw)),
                    as_of=date.fromisoformat(o["date"]),
                )
            except (InvalidOperation, ValueError):
                continue
        raise RuntimeError(f"FRED {series_id}: no usable observations returned")
