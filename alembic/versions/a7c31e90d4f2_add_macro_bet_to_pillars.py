"""add macro_bet to pillars

Maps the 13 pillars into ~9 "effective bets" — the factor-level grouping used by
the Risk page. The mapping is data (this migration backfills it); the per-bet
scenario assumptions are policy and live in app/risk_config.py.

Revision ID: a7c31e90d4f2
Revises: f2c58e67ea91
Create Date: 2026-08-30
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c31e90d4f2'
down_revision: str | None = 'f2c58e67ea91'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# pillar id -> macro bet key (keys defined in app/risk_config.py)
BACKFILL: dict[str, str] = {
    "P01": "ai_compute_supply",      # AI Semis & Chip Supply
    "P02": "dc_power_neoclouds",     # Data Center, Power & Connectivity
    "P03": "dc_power_neoclouds",     # Neoclouds & AI Compute
    "P04": "hyperscalers",           # Mag 7 / Hyperscalers
    "P05": "ai_software_data",       # Enterprise Software & Data
    "P06": "ai_software_data",       # Cyber & GovTech
    "P07": "ai_software_data",       # Applied / Vertical AI
    "P08": "quantum",                # Quantum Computing
    "P09": "space_defense",          # Space & Aero Supply
    "P10": "dc_power_neoclouds",     # Clean Energy / Nuclear (demand = DC buildout)
    "P11": "consumer_internet",      # Digital Entertainment & Consumer Internet
    "P12": "fintech",                # Fintech & Digital Finance
    "P13": "ev_mobility",            # EV & Mobility
}


def upgrade() -> None:
    op.add_column('pillars', sa.Column('macro_bet', sa.Text(), nullable=True))
    for pillar_id, bet in BACKFILL.items():
        op.execute(
            sa.text("UPDATE pillars SET macro_bet = :bet WHERE id = :pid")
            .bindparams(bet=bet, pid=pillar_id)
        )


def downgrade() -> None:
    op.drop_column('pillars', 'macro_bet')
