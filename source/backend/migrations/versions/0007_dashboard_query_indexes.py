"""Add dashboard range-query indexes.

Revision ID: 0007_dashboard_query_indexes
Revises: 0006_live_connection_state
"""
from collections.abc import Sequence
from alembic import op

revision: str = "0007_dashboard_query_indexes"
down_revision: str | None = "0006_live_connection_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.create_index("ix_energy_intervals_source_metric_range", "energy_intervals", ["source_id", "metric_code", "interval_start", "interval_end"])
    op.create_index("ix_tariff_rates_lookup", "tariff_rates", ["fuel", "direction", "rate_kind", "valid_from", "valid_to"])

def downgrade() -> None:
    op.drop_index("ix_tariff_rates_lookup", table_name="tariff_rates")
    op.drop_index("ix_energy_intervals_source_metric_range", table_name="energy_intervals")
