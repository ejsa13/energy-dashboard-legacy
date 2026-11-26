"""Index tariff intervals used by dashboard pricing.

Revision ID: 0008_tariff_rate_range_index
Revises: 0007_dashboard_query_indexes
"""
from collections.abc import Sequence
from alembic import op

revision: str = "0008_tariff_rate_range_index"
down_revision: str | None = "0007_dashboard_query_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("""CREATE INDEX ix_tariff_rates_interval_lookup ON tariff_rates USING gist
      (fuel, direction, rate_kind,
       tstzrange(valid_from, coalesce(valid_to, 'infinity'::timestamptz), '[)'))""")

def downgrade() -> None:
    op.drop_index("ix_tariff_rates_interval_lookup", table_name="tariff_rates")
