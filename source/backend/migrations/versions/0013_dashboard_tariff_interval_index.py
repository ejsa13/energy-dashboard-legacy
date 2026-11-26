"""Index tenant-scoped tariff interval lookups.

Revision ID: 0013_tariff_interval_idx
Revises: 0012_scoped_query_indexes
"""
from collections.abc import Sequence
from alembic import op

revision: str = "0013_tariff_interval_idx"
down_revision: str | None = "0012_scoped_query_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""CREATE INDEX IF NOT EXISTS ix_tariff_rates_dashboard_interval_lookup
      ON tariff_rates USING gist (
        dashboard_id, fuel, direction, rate_kind,
        tstzrange(valid_from, coalesce(valid_to, 'infinity'::timestamptz), '[)')
      )""")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_tariff_rates_dashboard_interval_lookup")
