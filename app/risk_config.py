"""Risk-page policy: the effective-bet taxonomy and stress-scenario assumptions.

Deliberately a plain Python module, not env config — these numbers are decisions
the two managers agree on, and they belong in version control where a change is
visible in the diff. The pillar -> bet MAPPING is data and lives on the
`pillars` table (backfilled by migration a7c31e90d4f2); everything ABOUT a bet
is defined here.

The scenario: a "capex air-pocket" — two of the Big-4 hyperscalers guide capex
flat-to-down and credit sentiment sours. Drawdown assumptions per bet are
illustrative, loosely calibrated to how each group traded in the July-2026
chip selloff. They are inputs to a what-if, not forecasts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class BetMeta:
    key: str
    label: str
    scenario_drawdown: Decimal   # fraction of market value lost in the scenario
    in_ai_complex: bool          # counts toward the "one macro bet" aggregate
    order: int                   # display order on the Risk page


BETS: dict[str, BetMeta] = {m.key: m for m in [
    BetMeta("ai_compute_supply",  "AI compute supply (semis & equipment)",
            Decimal("0.40"), True,  1),
    BetMeta("hyperscalers",       "Hyperscaler platforms",
            Decimal("0.20"), True,  2),
    BetMeta("ai_software_data",   "AI-era software & data",
            Decimal("0.25"), True,  3),
    BetMeta("dc_power_neoclouds", "DC power, neoclouds & nuclear",
            Decimal("0.45"), True,  4),
    BetMeta("consumer_internet",  "Consumer internet & entertainment",
            Decimal("0.15"), False, 5),
    BetMeta("space_defense",      "Space & defense supply",
            Decimal("0.15"), False, 6),
    BetMeta("fintech",            "Fintech",
            Decimal("0.15"), False, 7),
    BetMeta("quantum",            "Quantum",
            Decimal("0.35"), False, 8),
    BetMeta("ev_mobility",        "EV & mobility",
            Decimal("0.15"), False, 9),
]}

# Positions whose pillar has no macro_bet (or no pillar at all) fall here, so a
# newly-added pillar is visible on the Risk page instead of silently excluded.
UNMAPPED_KEY = "unmapped"
UNMAPPED = BetMeta(UNMAPPED_KEY, "— unmapped —", Decimal("0.15"), False, 99)

SCENARIO_NAME = "Capex air-pocket"
SCENARIO_NOTE = (
    "Two of the Big-4 guide 2027 capex flat-to-down and credit sentiment sours. "
    "Per-bet drawdowns are agreed assumptions (edit in app/risk_config.py), "
    "calibrated loosely to the July-2026 chip selloff. Illustrative, not a forecast."
)


def bet_for(macro_bet_key: str | None) -> BetMeta:
    if macro_bet_key is None:
        return UNMAPPED
    return BETS.get(macro_bet_key, UNMAPPED)


# ---------------------------------------------------------------------------
# Zone 1 — macro gauges. Policy only; fetching/eval lives in app/gauges.py.
#
# Each component maps a raw input to a 0–100 score by piecewise-linear
# interpolation through a (p0 → 0, p50 → 50, p100 → 100) triple, clamped at
# the ends. The triple may run in either direction — for the risk-appetite
# gauge a FALLING VIX scores higher. Weights renormalize over the components
# that actually fetched, so one failed input degrades the gauge visibly
# instead of zeroing it.
#
# Turkey is deliberately excluded from the credit gauge (per the managers) —
# this is a US-funding/credit monitor, not an EM one.
#
# kinds: fred        — latest FRED observation (params: series)
#        fmp_level   — FMP quote price (params: symbol)
#        fmp_spread  — day-change difference a−b, as a fraction, computed from
#                      price/prev_close (params: a, b)
#        fmp_daychg  — one symbol's day change, as a fraction (params: symbol)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GaugeComponent:
    key: str
    label: str
    kind: str                 # fred | fmp_level | fmp_spread | fmp_daychg
    params: tuple             # (series,) or (symbol,) or (a, b)
    p0: Decimal
    p50: Decimal
    p100: Decimal
    weight: Decimal
    fmt: str                  # python format spec for the raw value


@dataclass(frozen=True)
class GaugeDef:
    key: str
    label: str
    higher_means: str         # shown on the card, e.g. "higher = more stress"
    components: tuple
    # bands evaluated top-down: (min_score or None, label, css) — None matches all
    bands: tuple


GAUGES: dict[str, GaugeDef] = {g.key: g for g in [
    GaugeDef(
        key="stress", label="Stress composite", higher_means="higher = more stress",
        components=(
            GaugeComponent("hy_oas", "HY OAS", "fred", ("BAMLH0A0HYM2",),
                           Decimal("3.0"), Decimal("5.0"), Decimal("8.0"), Decimal("0.35"), "{:.2f}%"),
            GaugeComponent("ig_oas", "IG OAS", "fred", ("BAMLC0A0CM",),
                           Decimal("1.0"), Decimal("1.5"), Decimal("2.5"), Decimal("0.15"), "{:.2f}%"),
            GaugeComponent("vix", "VIX", "fmp_level", ("^VIX",),
                           Decimal("15"), Decimal("25"), Decimal("40"), Decimal("0.30"), "{:.1f}"),
            GaugeComponent("stlfsi", "STLFSI (weekly)", "fred", ("STLFSI4",),
                           Decimal("-0.5"), Decimal("0.5"), Decimal("2.5"), Decimal("0.20"), "{:+.2f}"),
        ),
        bands=((Decimal("50"), "stressed", "alert"),
               (Decimal("25"), "elevated", "watch"),
               (None, "calm", "ok")),
    ),
    GaugeDef(
        key="appetite", label="Risk appetite", higher_means="higher = risk-on",
        components=(
            GaugeComponent("vix", "VIX (inverted)", "fmp_level", ("^VIX",),
                           Decimal("40"), Decimal("25"), Decimal("15"), Decimal("0.40"), "{:.1f}"),
            GaugeComponent("credit", "HYG−IEF day spread", "fmp_spread", ("HYG", "IEF"),
                           Decimal("-0.010"), Decimal("0"), Decimal("0.010"), Decimal("0.30"), "{:+.2%}"),
            GaugeComponent("breadth", "RSP−SPY day spread", "fmp_spread", ("RSP", "SPY"),
                           Decimal("-0.005"), Decimal("0"), Decimal("0.005"), Decimal("0.30"), "{:+.2%}"),
        ),
        bands=((Decimal("60"), "risk-on", "ok"),
               (Decimal("40"), "neutral", "watch"),
               (None, "risk-off", "alert")),
    ),
    GaugeDef(
        key="credit", label="Credit stress (US, ex-Turkey)", higher_means="higher = worse",
        components=(
            GaugeComponent("hy_oas", "HY OAS", "fred", ("BAMLH0A0HYM2",),
                           Decimal("3.0"), Decimal("5.0"), Decimal("8.0"), Decimal("0.45"), "{:.2f}%"),
            GaugeComponent("ig_oas", "IG OAS", "fred", ("BAMLC0A0CM",),
                           Decimal("1.0"), Decimal("1.5"), Decimal("2.5"), Decimal("0.20"), "{:.2f}%"),
            GaugeComponent("hyg_day", "HYG day move", "fmp_daychg", ("HYG",),
                           Decimal("0"), Decimal("-0.010"), Decimal("-0.025"), Decimal("0.20"), "{:+.2%}"),
            GaugeComponent("vix", "VIX", "fmp_level", ("^VIX",),
                           Decimal("15"), Decimal("25"), Decimal("40"), Decimal("0.15"), "{:.1f}"),
        ),
        bands=((Decimal("50"), "crisis risk", "alert"),
               (Decimal("25"), "elevated", "watch"),
               (None, "calm", "ok")),
    ),
]}

GAUGE_STALE_DAYS = 3   # badge the card when the latest point is older than this


# ---------------------------------------------------------------------------
# Risk budget — limits, not just tripwires. Evaluated in app/risk_budget.py,
# surfaced on the Risk page and fired on the Signals page.
#
# AI-complex cap: the in_ai_complex bets above, summed, as a share of NAV (the
# whole fund incl. cash, so raising cash is a valid way back under the cap).
# Monthly-loss trigger: the fund's NAV-per-share return month-to-date, or over
# the trailing 30 days (which catches a loss straddling a month-end), at or
# below the threshold means the de-risking step below is due.
#
# "warn" fires inside WARN_BAND of a limit so a breach is never a surprise.
# ---------------------------------------------------------------------------

AI_COMPLEX_CAP = Decimal("0.80")            # max AI-complex market value / NAV
AI_COMPLEX_WARN_BAND = Decimal("0.03")      # warn from cap - 3pp
MONTHLY_LOSS_TRIGGER = Decimal("-0.08")     # MTD or trailing-30d NAV return
MONTHLY_LOSS_WARN_BAND = Decimal("0.02")    # warn from trigger + 2pp
NAV_STALE_DAYS = 7                          # a NAV older than this can't clear the loss rule

AI_COMPLEX_ACTION = (
    "Trim AI-complex names back under the cap (start with the largest bet over its "
    "share), or raise cash — no new AI-complex adds until back under."
)
DERISK_ACTION = (
    "De-risking step: cut gross by ~10% of NAV into cash, starting with the "
    "highest-scenario-drawdown bets; no new adds until the next monthly review."
)


# ---------------------------------------------------------------------------
# Snapshot cadence. Monthly is the minimum: the FLOW/DISCRETIONARY classifier
# and the trading-alpha counterfactual both degrade as the interval between
# custodian files widens (round trips inside the gap are invisible).
# ---------------------------------------------------------------------------

SNAPSHOT_DUE_DAYS = 28      # amber: the monthly file is due
SNAPSHOT_STALE_DAYS = 35    # red: past the monthly minimum


def snapshot_age_class(days: int | None) -> str:
    """CSS class for a custodian-file age badge: age-fresh | age-warn | age-stale."""
    if days is None or days > SNAPSHOT_STALE_DAYS:
        return "age-stale"
    if days > SNAPSHOT_DUE_DAYS:
        return "age-warn"
    return "age-fresh"
