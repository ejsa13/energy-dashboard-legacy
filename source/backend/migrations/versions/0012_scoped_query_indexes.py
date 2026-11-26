"""Add indexes for dashboard-scoped dashboard and importer reads.

Revision ID: 0012_scoped_query_indexes
Revises: 0011_projection_system_position
"""
from collections.abc import Sequence
from alembic import op

revision: str = "0012_scoped_query_indexes"
down_revision: str | None = "0011_projection_system_position"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE INDEX IF NOT EXISTS ix_energy_intervals_source_metric_start ON energy_intervals (source_id, metric_code, interval_start)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_tariff_rates_dashboard_lookup ON tariff_rates (dashboard_id, fuel, direction, rate_kind, valid_from DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_coverage_windows_source_dataset_status_start ON coverage_windows (source_id, dataset, status, window_start)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_sync_checkpoints_source_dataset ON sync_checkpoints (source_id, dataset)")


def downgrade() -> None:
    for name in ("ix_sync_checkpoints_source_dataset", "ix_coverage_windows_source_dataset_status_start", "ix_tariff_rates_dashboard_lookup", "ix_energy_intervals_source_metric_start"):
        op.execute(f"DROP INDEX IF EXISTS {name}")
