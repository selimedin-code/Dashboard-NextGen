"""add gauge_points table

Daily history of the three macro gauges (stress composite, risk appetite,
credit crisis). One row per gauge per day; components stored as JSONB so the
card can show the breakdown and a parser change never needs a re-fetch.

Revision ID: d9f4a6c210b3
Revises: c4e8b21f7a90
Create Date: 2026-08-30
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'd9f4a6c210b3'
down_revision: str | None = 'c4e8b21f7a90'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'gauge_points',
        sa.Column('gauge', sa.Text(), nullable=False),
        sa.Column('as_of', sa.Date(), nullable=False),
        sa.Column('value', sa.Numeric(), nullable=True),     # NULL = fetch failed
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('components', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('gauge', 'as_of'),
    )


def downgrade() -> None:
    op.drop_table('gauge_points')
