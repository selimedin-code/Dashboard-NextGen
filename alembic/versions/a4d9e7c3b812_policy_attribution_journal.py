"""policy benchmark targets, Brinson attribution, closed-loop journal

pillars.target_weight, attribution_period, and the expected-outcome / horizon /
verdict columns on review_log.

Revision ID: a4d9e7c3b812
Revises: f3b8d2a61c47
Create Date: 2026-09-19
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'a4d9e7c3b812'
down_revision: str | None = 'f3b8d2a61c47'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('pillars', sa.Column('target_weight', sa.Numeric(8, 6), nullable=True))

    op.add_column('review_log', sa.Column('expected_outcome', sa.Text(), nullable=True))
    op.add_column('review_log', sa.Column('horizon_date', sa.Date(), nullable=True))
    op.add_column('review_log', sa.Column('invalidation', sa.Text(), nullable=True))
    op.add_column('review_log', sa.Column('scored_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('review_log', sa.Column('verdict', sa.Text(), nullable=True))
    op.add_column('review_log', sa.Column('verdict_note', sa.Text(), nullable=True))
    op.create_index('ix_review_log_horizon_date', 'review_log', ['horizon_date'])

    op.create_table(
        'attribution_period',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('from_snapshot', sa.BigInteger(), nullable=False),
        sa.Column('to_snapshot', sa.BigInteger(), nullable=False),
        sa.Column('pillar', sa.Text(), nullable=False),
        sa.Column('port_weight', sa.Numeric(12, 8), nullable=False),
        sa.Column('bench_weight', sa.Numeric(12, 8), nullable=False),
        sa.Column('port_return', sa.Numeric(12, 8), nullable=True),
        sa.Column('bench_return', sa.Numeric(12, 8), nullable=True),
        sa.Column('allocation', sa.Numeric(12, 8), nullable=False),
        sa.Column('selection', sa.Numeric(12, 8), nullable=False),
        sa.Column('interaction', sa.Numeric(12, 8), nullable=False),
        sa.ForeignKeyConstraint(['from_snapshot'], ['snapshots.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['to_snapshot'], ['snapshots.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('from_snapshot', 'to_snapshot', 'pillar', name='uq_attribution_pair_pillar'),
    )


def downgrade() -> None:
    op.drop_table('attribution_period')
    op.drop_index('ix_review_log_horizon_date', table_name='review_log')
    for c in ('verdict_note', 'verdict', 'scored_at', 'invalidation', 'horizon_date', 'expected_outcome'):
        op.drop_column('review_log', c)
    op.drop_column('pillars', 'target_weight')
