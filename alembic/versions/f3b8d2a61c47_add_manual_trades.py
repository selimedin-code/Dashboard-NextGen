"""add snapshots.source + manual_trades

Single-ticker trades entered by hand between custodian files. Each lives on a
"manual" snapshot = previous snapshot with its trades replayed.

Revision ID: f3b8d2a61c47
Revises: e1a7b3c9d052
Create Date: 2026-09-19
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'f3b8d2a61c47'
down_revision: str | None = 'e1a7b3c9d052'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('snapshots', sa.Column('source', sa.Text(), nullable=False,
                                         server_default='upload'))
    op.create_table(
        'manual_trades',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('snapshot_id', sa.BigInteger(), nullable=False),
        sa.Column('ticker', sa.Text(), nullable=False),
        sa.Column('raw_ticker', sa.Text(), nullable=False),
        sa.Column('side', sa.Text(), nullable=False),
        sa.Column('units', sa.Numeric(20, 6), nullable=False),
        sa.Column('price', sa.Numeric(20, 6), nullable=False),
        sa.Column('price_source', sa.Text(), nullable=False),
        sa.Column('adjust_cash', sa.Boolean(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['snapshot_id'], ['snapshots.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_manual_trades_snapshot_id', 'manual_trades', ['snapshot_id'])


def downgrade() -> None:
    op.drop_index('ix_manual_trades_snapshot_id', table_name='manual_trades')
    op.drop_table('manual_trades')
    op.drop_column('snapshots', 'source')
