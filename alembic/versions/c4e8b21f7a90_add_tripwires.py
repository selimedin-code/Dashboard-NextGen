"""add tripwires table and seed the Section-3 rule set

Name-level exit rules with three modes:
  auto   — evaluated at read time from cached data (no network)
  semi   — quarterly readings entered after each print; flagged "data due"
           when a holding reports near/after the last update
  manual — event rules toggled by hand (launch outcome, rating action)

Revision ID: c4e8b21f7a90
Revises: a7c31e90d4f2
Create Date: 2026-08-30
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4e8b21f7a90'
down_revision: str | None = 'a7c31e90d4f2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# (ticker, rule, action, mode, metric, threshold, direction)
SEED: list[tuple] = [
    ("PLTR", "US commercial revenue growth < 80% y/y",
     "Exit remainder", "semi", None, None, None),
    ("TSLA", "Robotaxi rollout stalls / fleet growth flat for two quarters",
     "Exit remainder", "manual", None, None, None),
    ("RDDT", "US DAU declines two consecutive quarters",
     "Exit remainder", "semi", None, None, None),
    ("ORCL", "Downgrade to junk, or FY27 FCF guidance worsens",
     "Exit remainder", "manual", None, None, None),
    ("SOUN", "2027 revenue target ($350-400M) cut",
     "Exit remainder", "semi", None, None, None),
    ("RKLB", "Neutron launch failure",
     "Reassess sizing - no reflex sell", "manual", None, None, None),
    ("FLY", "2026 revenue guide ($420-450M) cut, or Alpha/Blue Ghost slip",
     "Exit", "semi", None, None, None),
    ("IREN", "Major AI-cloud contract cancellation or GPU-financing stress",
     "Exit", "manual", None, None, None),
    ("IONQ", "Share dilution > 15%/yr without revenue acceleration",
     "Cut to token position", "auto", "dilution_yoy", 0.15, "above"),
    ("MRVL", "Post-earnings review: custom-AI pipeline intact?",
     "Halve if guidance was cut", "semi", None, None, None),
    ("MSFT", "Free cash flow negative for 2+ quarters",
     "Trim", "semi", None, None, None),
    ("META", "Capex > 55% of sales for 2+ quarters",
     "Trim", "semi", None, None, None),
    (None, "Any Big-4 hyperscaler guides 2027 capex down",
     "Reduce compute-supply & DC/power bets by a pre-agreed step",
     "manual", None, None, None),
]


def upgrade() -> None:
    op.create_table(
        'tripwires',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('ticker', sa.Text(), nullable=True),   # NULL = portfolio-level
        sa.Column('rule', sa.Text(), nullable=False),
        sa.Column('action', sa.Text(), nullable=False),
        sa.Column('mode', sa.Text(), nullable=False),    # auto | semi | manual
        sa.Column('metric', sa.Text(), nullable=True),   # auto rules: computation key
        sa.Column('threshold', sa.Numeric(), nullable=True),
        sa.Column('direction', sa.Text(), nullable=True),  # above | below
        sa.Column('latest_value', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), nullable=False, server_default='ok'),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    tw = sa.table(
        'tripwires',
        sa.column('ticker', sa.Text()), sa.column('rule', sa.Text()),
        sa.column('action', sa.Text()), sa.column('mode', sa.Text()),
        sa.column('metric', sa.Text()), sa.column('threshold', sa.Numeric()),
        sa.column('direction', sa.Text()),
    )
    op.bulk_insert(tw, [
        {"ticker": t, "rule": r, "action": a, "mode": m,
         "metric": met, "threshold": th, "direction": d}
        for (t, r, a, m, met, th, d) in SEED
    ])


def downgrade() -> None:
    op.drop_table('tripwires')
