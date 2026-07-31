# Live Estimated Fund Price — Methodology

## Purpose

The fund is a UBS Actively Managed Certificate (ISIN CH1298091955). Its
**authoritative price is the NAV per share** that the issuer strikes — the value you
read off your statement (and that UBS also quotes live as a bid/ask on KeyInvest).
You enter those official prices on the **Performance** tab, and they drive YTD, the
curve, and all reported performance.

Between official prices, the **Live Estimated Price** gives a same-day proxy so the
number moves with the market on days you have not entered a statement price. It is a
convenience, never a replacement for the official NAV.

## The formula

    est_price(now) = P_official × ( V_live / V_official )

- **P_official** — your most recent entered fund price (NAV/share), struck on date **D**.
- **V_official** — market value of the fund's holdings basket priced at the **closing
  prices on D**.
- **V_live** — market value of the **same basket** priced at **current live prices**.

Both V's use the **same units** (from the latest holdings snapshot) and both include
cash, so `V_live / V_official` is the **pure price return of a fixed basket**. Because
the basket is identical on both sides, units and any shares-outstanding figure cancel —
no shares count is required.

Equivalently: `est_price = P_official × (1 + book_return_since_D)`.

## Inputs

| Input | Source |
|---|---|
| Units per holding | Latest custodian holdings snapshot |
| Cash | The `USD Cash` line of that snapshot |
| Closing price on date D | Cached daily price history per holding (FMP EOD) |
| Live prices | Latest quotes (FMP), updated by the **Refresh** button |
| P_official, D | Your latest entered fund price |

## Worked example

Basket (fixed units) + cash, last official price `P_official = 1,584.00` on `D = 30.07`:

- Value the basket at **30.07 closes** → `V_official`
- Value the **same** basket at **live prices** → `V_live`
- Suppose `V_live / V_official = 1.012` (the underlying holdings are +1.2% since 30.07)
- `est_price = 1,584.00 × 1.012 = 1,603.0`

The estimate re-bases every time you enter a new official price: the moment you record
a fresh statement price, `P_official` and `D` advance and the drift resets to zero.

## Assumptions and limitations

1. **Holdings unchanged since D.** Valid between statements. A trade, subscription or
   redemption between the statement date and now is not captured.
2. **Fees/expenses are excluded.** The AMC's NAV nets management fees as they accrue;
   the estimate does not, so it will read slightly high over time versus the official NAV.
3. **FX is proxied.** Non-USD lines are marked via a USD proxy (e.g. Infineon `IFX GR`
   via its US ADR `IFNNY`), so currency moves are approximate.
4. **Anchored to the official price.** The estimate carries no stale data forward — it is
   always `latest official × basket return`, so entering a new price corrects it fully.
5. **Suppressed when it cannot be trusted.** If the cached stock history does not reach D,
   marking from an earlier close would re-apply a move the official price already includes
   (a double-count), so the estimate is **hidden** rather than shown wrong. Refresh
   fundamentals to extend the history up to D and it returns.

## Relationship to the UBS quoted price

UBS publishes a live two-way price for the certificate, e.g.:

    Bid 1,595.31 USD   Ask 1,611.42 USD   →   mid ≈ 1,603.37,  spread ≈ 16.11 (~1.0%)

- The **mid** is the **issuer's own live fair value** (their live NAV estimate); the
  spread is the dealing cost, not a valuation range.
- The mid — or your statement NAV — is the **truth**. Our estimate is an **independent,
  bottom-up proxy** built from the underlying holdings.
- The two should track each other but will differ because of fees, any gap between the
  fund's real basket and our snapshot, the FX proxy, and timing.

### Practical guidance

- For the most accurate live figure, enter the **UBS mid = (Bid + Ask) / 2** as today's
  fund price on the Performance tab — that is the issuer's live NAV and becomes the new
  official anchor.
- Rely on the bottom-up **estimate** on days you do not check UBS; treat it as indicative
  (roughly ±the fee drift and FX-proxy error), not exact.
