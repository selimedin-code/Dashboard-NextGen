"""Price refresh: fetch live quotes into the quote_cache.

Symbol mapping: US listings quote under the bare ticker. Other listings need an
exchange-specific symbol that the current FMP plan does not serve, so they are
recorded as a visible failure unless an explicit override is provided (e.g. an
ADR). This is the one place external symbols are resolved.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.ingest.normalize import CASH_TICKER
from app.models import HoldingSnapshot, QuoteCache, Security, Snapshot
from app.providers.fmp import EntitlementError, FMPClient, Quote

# Manual ticker → FMP-symbol overrides for names whose primary listing the plan
# cannot quote. IFX GR (Infineon, Xetra) is served via its US ADR IFNNY.
FMP_SYMBOL_OVERRIDES: dict[str, str] = {
    "IFX": "IFNNY",
}


def resolve_fmp_symbol(ticker: str, exchange: str | None) -> str | None:
    """Return the symbol to query, or None if this ticker cannot be priced."""
    if ticker in FMP_SYMBOL_OVERRIDES:
        return FMP_SYMBOL_OVERRIDES[ticker]
    if exchange in (None, "US"):
        return ticker
    return None  # non-US listing, no override → cannot price on this plan


def held_tickers_with_exchange(session: Session) -> list[tuple[str, str | None]]:
    latest = session.execute(
        select(Snapshot).order_by(Snapshot.as_of.desc()).limit(1)
    ).scalar_one_or_none()
    if latest is None:
        return []
    rows = session.execute(
        select(HoldingSnapshot.ticker).where(HoldingSnapshot.snapshot_id == latest.id)
    ).scalars().all()
    exch = {
        s.ticker: s.exchange
        for s in session.execute(select(Security)).scalars().all()
    }
    return [(t, exch.get(t)) for t in rows if t != CASH_TICKER]


def refresh_quotes(session: Session, *, client: FMPClient | None = None) -> dict:
    """Fetch quotes for all held tickers and upsert the cache. Returns a summary.

    Cash is not quoted. One symbol per call (batch endpoint is off-plan); a single
    symbol failing never aborts the run.
    """
    holdings = held_tickers_with_exchange(session)
    owns_client = client is None
    if owns_client:
        key = get_settings().fmp_api_key
        if not key:
            raise RuntimeError("FMP_API_KEY is not set — cannot refresh prices.")
        client = FMPClient(key, min_interval=0.05)

    ok = failed = unpriceable = 0
    try:
        for ticker, exchange in holdings:
            symbol = resolve_fmp_symbol(ticker, exchange)
            if symbol is None:
                _upsert(session, ticker, None, None,
                        error=f"no live price: {exchange} listing not available on plan")
                unpriceable += 1
                continue
            try:
                q = client.get_quote(symbol)
                _upsert(session, ticker, symbol, q)
                ok += 1
            except EntitlementError as exc:
                _upsert(session, ticker, symbol, None, error=f"no live price: {exc}")
                unpriceable += 1
            except Exception as exc:  # noqa: BLE001 — one bad symbol must not abort the batch
                _upsert(session, ticker, symbol, None, error=f"fetch failed: {exc}")
                failed += 1
    finally:
        if owns_client:
            client.close()

    session.commit()
    return {"ok": ok, "unpriceable": unpriceable, "failed": failed, "total": len(holdings)}


def _upsert(session: Session, ticker: str, symbol: str | None, quote: Quote | None,
            *, error: str | None = None) -> None:
    row = session.get(QuoteCache, ticker)
    if row is None:
        row = QuoteCache(ticker=ticker)
        session.add(row)
    row.fmp_symbol = symbol
    row.fetched_at = datetime.now(timezone.utc)
    if quote is not None:
        row.price = quote.price
        row.prev_close = quote.prev_close
        row.day_change_pct = quote.day_change_pct
        row.currency = quote.currency
        row.name = quote.name
        row.ok = True
        row.error = None
    else:
        row.ok = False
        row.error = error


def load_quote_cache(session: Session) -> dict[str, QuoteCache]:
    return {q.ticker: q for q in session.execute(select(QuoteCache)).scalars().all()}


def cache_age_seconds(session: Session) -> float | None:
    newest = session.execute(
        select(QuoteCache.fetched_at).order_by(QuoteCache.fetched_at.desc()).limit(1)
    ).scalar_one_or_none()
    if newest is None:
        return None
    return (datetime.now(timezone.utc) - newest).total_seconds()
