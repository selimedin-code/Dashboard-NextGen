"""add review_log + review_attachment

Per-ticker review notes stamped with the price at the time, and attached report
files (bytes stored in Postgres — the Render web disk is ephemeral).

Revision ID: e1a7b3c9d052
Revises: d9f4a6c210b3
Create Date: 2026-09-19
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e1a7b3c9d052'
down_revision: str | None = 'd9f4a6c210b3'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'review_log',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('ticker', sa.Text(), nullable=False),
        sa.Column('review_date', sa.Date(), nullable=False),
        sa.Column('price', sa.Numeric(20, 6), nullable=True),
        sa.Column('price_source', sa.Text(), nullable=True),
        sa.Column('price_as_of', sa.DateTime(timezone=True), nullable=True),
        sa.Column('stance', sa.Text(), nullable=True),
        sa.Column('note', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_review_log_ticker', 'review_log', ['ticker'])
    op.create_table(
        'review_attachment',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('review_id', sa.BigInteger(), nullable=False),
        sa.Column('filename', sa.Text(), nullable=False),
        sa.Column('content_type', sa.Text(), nullable=True),
        sa.Column('size_bytes', sa.BigInteger(), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('uploaded_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['review_id'], ['review_log.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_review_attachment_review_id', 'review_attachment', ['review_id'])


def downgrade() -> None:
    op.drop_index('ix_review_attachment_review_id', table_name='review_attachment')
    op.drop_table('review_attachment')
    op.drop_index('ix_review_log_ticker', table_name='review_log')
    op.drop_table('review_log')
