"""Phase 4 data layer — cached company fundamentals, sourced from FMP.

Principles from nextgen_fundamentals_brief.md:
  - Cache everything; the detail page reads Postgres and never blocks on the API.
  - Partial failure is normal — one source failing must not lose the others, so
    source_status records what worked.
  - Store raw payloads alongside parsed columns, so a mapping bug is recoverable.
  - Nothing numeric ever comes from news text; news is awareness only.

The brief preferred Alpha Vantage COMPANY_OVERVIEW, but the FMP plan in use covers
profile + ratios-ttm + key-metrics-ttm (valuation/quality), price-target-consensus,
grades-consensus, analyst-estimates, earnings, historical prices and news — so
everything comes from one provider and one key.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    EarningsHistory,
    EstimatesSnapshot,
    FundamentalsSnapshot,
    NewsItem,
    PriceHistoryCache,
)
from app.prices import resolve_fmp_symbol
from app.providers.fmp import EntitlementError, FMPClient
from app.models import Security

HISTORY_KEEP = 420          # trading days of closes to cache (~20 months)
NEWS_KEEP = 20


def _dec(v) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _parse_range(rng: str | None) -> tuple[Decimal | None, Decimal | None]:
    """profile.range is like '164.07-236.54' -> (low, high)."""
    if not rng or "-" not in rng:
        return None, None
    lo, _, hi = rng.partition("-")
    return _dec(lo), _dec(hi)


def _first(data):
    return data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else None)


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def refresh_ticker(session: Session, ticker: str, *, client: FMPClient | None = None) -> dict:
    """Fetch every source for one ticker and upsert the cache tables.

    Returns source_status. Never raises on a single source; raises only if the
    symbol cannot be resolved at all. Caller commits.
    """
    sec = session.get(Security, ticker)
    symbol = resolve_fmp_symbol(ticker, sec.exchange if sec else None)
    if symbol is None:
        raise RuntimeError(f"{ticker}: no FMP symbol (non-US listing not on plan).")

    owns = client is None
    if owns:
        key = get_settings().fmp_api_key
        if not key:
            raise RuntimeError("FMP_API_KEY is not set.")
        client = FMPClient(key, min_interval=0.05)

    status: dict[str, str] = {}
    today = date.today()

    def pull(name, path, **params):
        try:
            data = client.request(path, **params)
            status[name] = "ok"
            return data
        except EntitlementError as exc:
            status[name] = f"unavailable: {exc}"
        except Exception as exc:  # noqa: BLE001
            status[name] = f"error: {exc}"
        return None

    try:
        profile = _first(pull("profile", "profile", symbol=symbol))
        ratios = _first(pull("ratios", "ratios-ttm", symbol=symbol))
        metrics = _first(pull("key_metrics", "key-metrics-ttm", symbol=symbol))
        target = _first(pull("targets", "price-target-consensus", symbol=symbol))
        grades = _first(pull("grades", "grades-consensus", symbol=symbol))
        estimates = pull("estimates", "analyst-estimates", symbol=symbol, period="annual", limit="5") or []
        earnings = pull("earnings", "earnings", symbol=symbol, limit="12") or []
        history = pull("history", "historical-price-eod/light", symbol=symbol) or []
        news = pull("news", "news/stock", symbols=symbol, limit=str(NEWS_KEEP)) or []
    finally:
        if owns:
            client.close()

    series = _store_history(session, ticker, history)
    dma50 = _dma(series, 50)
    dma200 = _dma(series, 200)

    _upsert_fundamentals(session, ticker, today, profile, ratios, metrics, target, grades,
                         dma50, dma200, status)
    _upsert_estimates(session, ticker, today, estimates)
    _upsert_earnings(session, ticker, earnings)
    _upsert_news(session, ticker, news)

    session.commit()
    return status


def refresh_all_fundamentals(session: Session, tickers: list[str] | None = None) -> dict:
    """Refresh fundamentals for every held ticker (or a given list), reusing one
    client. One ticker failing never aborts the batch. Returns a summary."""
    from app.prices import held_tickers_with_exchange

    if tickers is None:
        tickers = [t for t, _ in held_tickers_with_exchange(session)]

    key = get_settings().fmp_api_key
    if not key:
        raise RuntimeError("FMP_API_KEY is not set.")
    client = FMPClient(key, min_interval=0.03)
    ok = failed = 0
    errors: list[str] = []
    try:
        for t in tickers:
            try:
                refresh_ticker(session, t, client=client)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                # Roll back so a failed ticker never poisons the next one's transaction.
                session.rollback()
                failed += 1
                errors.append(f"{t}: {str(exc)[:120]}")
    finally:
        client.close()
    return {"ok": ok, "failed": failed, "total": len(tickers), "errors": errors[:10]}


def _store_history(session: Session, ticker: str, history: list) -> list[tuple[str, Decimal]]:
    # FMP returns newest-first; keep the most recent HISTORY_KEEP, store oldest-first.
    pts: list[tuple[str, Decimal]] = []
    for row in history[:HISTORY_KEEP]:
        c = _dec(row.get("price"))
        d = row.get("date")
        if c is not None and d:
            pts.append((d, c))
    pts.reverse()
    if not pts:
        return []
    row = session.get(PriceHistoryCache, ticker)
    payload = [[d, str(c)] for d, c in pts]
    if row is None:
        session.add(PriceHistoryCache(ticker=ticker, series=payload,
                                      fetched_at=datetime.now(timezone.utc)))
    else:
        row.series = payload
        row.fetched_at = datetime.now(timezone.utc)
    return pts


def _dma(series: list[tuple[str, Decimal]], n: int) -> Decimal | None:
    if len(series) < n:
        return None
    closes = [c for _, c in series[-n:]]
    return sum(closes) / Decimal(n)


def _upsert_fundamentals(session, ticker, as_of, profile, ratios, metrics, target, grades,
                         dma50, dma200, status) -> None:
    row = session.execute(
        select(FundamentalsSnapshot).where(
            FundamentalsSnapshot.ticker == ticker, FundamentalsSnapshot.as_of == as_of
        )
    ).scalar_one_or_none()
    if row is None:
        row = FundamentalsSnapshot(ticker=ticker, as_of=as_of)
        session.add(row)
    row.fetched_at = datetime.now(timezone.utc)

    p, r, m = profile or {}, ratios or {}, metrics or {}
    low52, high52 = _parse_range(p.get("range"))
    mc = _dec(p.get("marketCap"))
    price = _dec(p.get("price"))

    row.company_name = p.get("companyName")
    row.sector = p.get("sector")
    row.industry = p.get("industry")
    row.exchange = p.get("exchange")
    row.market_cap = mc
    row.pe_trailing = _dec(r.get("priceToEarningsRatioTTM"))
    row.peg = _dec(r.get("priceToEarningsGrowthRatioTTM"))
    row.price_to_sales = _dec(r.get("priceToSalesRatioTTM"))
    row.price_to_book = _dec(r.get("priceToBookRatioTTM"))
    row.ev_to_revenue = _dec(m.get("evToSalesTTM"))
    row.ev_to_ebitda = _dec(m.get("evToEBITDATTM"))
    row.profit_margin = _dec(r.get("netProfitMarginTTM"))
    row.operating_margin = _dec(r.get("operatingProfitMarginTTM"))
    row.roa = _dec(m.get("returnOnAssetsTTM"))
    row.roe = _dec(m.get("returnOnEquityTTM"))
    row.eps_ttm = _dec(r.get("netIncomePerShareTTM"))
    row.beta = _dec(p.get("beta"))
    row.week52_low = low52
    row.week52_high = high52
    row.dma_50 = dma50
    row.dma_200 = dma200
    row.shares_outstanding = (mc / price) if (mc and price) else None

    if target:
        row.target_high = _dec(target.get("targetHigh"))
        row.target_low = _dec(target.get("targetLow"))
        row.target_consensus = _dec(target.get("targetConsensus"))
        row.target_median = _dec(target.get("targetMedian"))
        row.raw_fmp_targets = target
    if grades:
        row.grades_strong_buy = grades.get("strongBuy")
        row.grades_buy = grades.get("buy")
        row.grades_hold = grades.get("hold")
        row.grades_sell = grades.get("sell")
        row.grades_strong_sell = grades.get("strongSell")
        row.grades_consensus = grades.get("consensus")
        row.raw_fmp_grades = grades
    row.raw_av_overview = {"profile": profile, "ratios": ratios, "key_metrics": metrics}
    row.source_status = status


def _upsert_estimates(session, ticker, as_of, estimates) -> None:
    session.execute(delete(EstimatesSnapshot).where(
        EstimatesSnapshot.ticker == ticker, EstimatesSnapshot.as_of == as_of,
        EstimatesSnapshot.period_type == "annual",
    ))
    for e in estimates:
        d = e.get("date")
        if not d:
            continue
        session.add(EstimatesSnapshot(
            ticker=ticker, as_of=as_of, period_end=date.fromisoformat(d[:10]),
            period_type="annual",
            revenue_avg=_dec(e.get("revenueAvg")), revenue_low=_dec(e.get("revenueLow")),
            revenue_high=_dec(e.get("revenueHigh")), ebitda_avg=_dec(e.get("ebitdaAvg")),
            net_income_avg=_dec(e.get("netIncomeAvg")), eps_avg=_dec(e.get("epsAvg")),
            eps_low=_dec(e.get("epsLow")), eps_high=_dec(e.get("epsHigh")),
            num_analysts_rev=e.get("numAnalystsRevenue"), num_analysts_eps=e.get("numAnalystsEps"),
        ))


def _upsert_earnings(session, ticker, earnings) -> None:
    # Dedupe within the payload first — a provider occasionally returns two rows
    # for the same fiscal date (e.g. XNDU), which would violate the unique key.
    latest: dict[date, dict] = {}
    for e in earnings:
        d = e.get("date")
        if not d:
            continue
        fiscal = date.fromisoformat(d[:10])
        prev = latest.get(fiscal)
        # Prefer the row that has an actual EPS (a reported quarter over a stub).
        if prev is None or (e.get("epsActual") is not None and prev.get("epsActual") is None):
            latest[fiscal] = e

    for fiscal, e in latest.items():
        actual = _dec(e.get("epsActual"))
        est = _dec(e.get("epsEstimated"))
        surprise = ((actual - est) / abs(est) * 100) if (actual is not None and est) else None
        row = session.execute(select(EarningsHistory).where(
            EarningsHistory.ticker == ticker, EarningsHistory.fiscal_ending == fiscal
        )).scalar_one_or_none()
        if row is None:
            row = EarningsHistory(ticker=ticker, fiscal_ending=fiscal)
            session.add(row)
        row.eps_actual = actual
        row.eps_estimate = est
        row.surprise_pct = surprise
        row.updated_at = datetime.now(timezone.utc)


def _upsert_news(session, ticker, news) -> None:
    for n in news[:NEWS_KEEP]:
        url = n.get("url")
        if not url:
            continue
        exists = session.execute(select(NewsItem).where(
            NewsItem.ticker == ticker, NewsItem.url == url
        )).scalar_one_or_none()
        if exists:
            continue
        pub = n.get("publishedDate")
        published = None
        if pub:
            try:
                published = datetime.fromisoformat(pub).replace(tzinfo=timezone.utc)
            except ValueError:
                published = None
        session.add(NewsItem(
            ticker=ticker, url=url, title=n.get("title"),
            source=n.get("publisher") or n.get("site"), published_at=published,
            summary=(n.get("text") or "")[:600] or None,
        ))
