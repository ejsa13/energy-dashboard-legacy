"""Store installer-projected system costs and cumulative net positions.

Revision ID: 0011_projection_system_position
Revises: 0010_dashboard_data_scope
"""
from collections.abc import Sequence

from alembic import op


revision: str = "0011_projection_system_position"
down_revision: str | None = "0010_dashboard_data_scope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE projection_targets ADD COLUMN IF NOT EXISTS cost_gbp numeric(16,2)")
    op.execute("ALTER TABLE projection_targets ADD COLUMN IF NOT EXISTS net_position_gbp numeric(16,2)")


def downgrade() -> None:
    op.execute("ALTER TABLE projection_targets DROP COLUMN IF EXISTS net_position_gbp")
    op.execute("ALTER TABLE projection_targets DROP COLUMN IF EXISTS cost_gbp")
