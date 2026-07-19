"""Financial Modeling Prep client — live quotes for the position table.

Ported from the Dashboard-Edinos FMPProvider, trimmed to what Phase 3 needs
(one quote endpoint). The stable `/quote` endpoint is per-symbol; the batch
endpoint is not on the current plan, so callers fetch one symbol at a time and
cache the result. Uses httpx (already a dependency).

Errors are typed so the caller can tell "your plan lacks this / bad ticker"
(don't retry) apart from "rate limited / transient" (do retry).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

BASE = "https://financialmodelingprep.com/stable"


class EntitlementError(RuntimeError):
    """HTTP 402/403 or a plan message — the symbol/endpoint is not available. Never retry."""


class RateLimitError(RuntimeError):
    """HTTP 429 — retry with backoff."""


@dataclass
class Quote:
    symbol: str
    price: Decimal
    prev_close: Decimal | None
    day_change_pct: Decimal | None
    currency: str | None
    name: str | None
    as_of: datetime


def _dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


class FMPClient:
    def __init__(self, api_key: str, *, timeout: float = 15.0, max_retries: int = 4,
                 min_interval: float = 0.0):
        if not api_key:
            raise RuntimeError("FMP_API_KEY is not set.")
        self._key = api_key
        self._timeout = timeout
        self._max_retries = max_retries
        self._min_interval = min_interval
        self._last_call = 0.0
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> FMPClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _get(self, path: str, **params):
        params["apikey"] = self._key
        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            if self._min_interval:
                wait = self._min_interval - (time.monotonic() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
            try:
                r = self._client.get(f"{BASE}/{path}", params=params)
                self._last_call = time.monotonic()
                if r.status_code in (401, 402, 403):
                    raise EntitlementError(f"{path} {params.get('symbol','')} HTTP {r.status_code}")
                if r.status_code == 429:
                    ra = r.headers.get("Retry-After")
                    time.sleep(float(ra) if (ra and ra.isdigit()) else min(20.0, 4.0 * (attempt + 1)))
                    last_exc = RateLimitError("429")
                    continue
                r.raise_for_status()
                data = r.json()
                if isinstance(data, dict) and ("Error Message" in data or "error" in data):
                    msg = data.get("Error Message") or str(data.get("error"))
                    raise EntitlementError(f"{path}: {msg}")
                return data
            except EntitlementError:
                raise
            except (httpx.HTTPError, RateLimitError, ValueError) as exc:
                last_exc = exc
                time.sleep(1.0 * (attempt + 1))
        raise RuntimeError(f"{path} failed after {self._max_retries} tries: {last_exc}")

    def get_quote(self, symbol: str) -> Quote:
        """Fetch one quote. Raises EntitlementError for unavailable symbols."""
        rows = self._get("quote", symbol=symbol)
        if not rows:
            raise RuntimeError(f"{symbol}: empty quote (bad ticker?)")
        q = rows[0]
        price = _dec(q.get("price"))
        if price is None:
            raise RuntimeError(f"{symbol}: quote has no price")
        ts = q.get("timestamp")
        as_of = datetime.fromtimestamp(ts, timezone.utc) if ts else datetime.now(timezone.utc)
        return Quote(
            symbol=symbol,
            price=price,
            prev_close=_dec(q.get("previousClose")),
            day_change_pct=_dec(q.get("changePercentage")),
            currency=q.get("currency"),
            name=q.get("name"),
            as_of=as_of,
        )
