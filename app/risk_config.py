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
