"""Scope sources and tariff records to a dashboard.

Existing installations retain their old, unassigned records for archival. New
dashboard records always carry a dashboard id and API queries only read that
scope. This supports a clean production rollout without copying a previous
single-user household into a new account.
"""
from collections.abc import Sequence
from alembic import op

revision: str = "0010_dashboard_data_scope"
down_revision: str | None = "0009_multitenant_auth_setup"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE data_sources ADD COLUMN IF NOT EXISTS dashboard_id uuid REFERENCES dashboard_accounts(id) ON DELETE CASCADE")
    op.execute("CREATE INDEX IF NOT EXISTS ix_data_sources_dashboard_id ON data_sources(dashboard_id)")
    op.execute("ALTER TABLE data_sources DROP CONSTRAINT IF EXISTS data_sources_code_key")
    op.execute("ALTER TABLE data_sources ADD CONSTRAINT uq_data_sources_dashboard_code UNIQUE (dashboard_id, code)")
    op.execute("ALTER TABLE tariff_rates ADD COLUMN IF NOT EXISTS dashboard_id uuid REFERENCES dashboard_accounts(id) ON DELETE CASCADE")
    op.execute("CREATE INDEX IF NOT EXISTS ix_tariff_rates_dashboard_id ON tariff_rates(dashboard_id)")
    op.execute("ALTER TABLE tariff_rates DROP CONSTRAINT IF EXISTS uq_tariff_rates_identity")
    op.execute("ALTER TABLE tariff_rates ADD CONSTRAINT uq_tariff_rates_identity UNIQUE (dashboard_id, fuel, direction, tariff_code, rate_kind, valid_from)")

    # Defence in depth: a future raw query cannot read a dashboard's source or
    # tariffs unless it explicitly establishes the tenant in its transaction.
    op.execute("ALTER TABLE data_sources ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tariff_rates ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE data_sources FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tariff_rates FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY dashboard_sources_isolation ON data_sources USING (dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid) WITH CHECK (dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid)")
    op.execute("CREATE POLICY dashboard_tariffs_isolation ON tariff_rates USING (dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid) WITH CHECK (dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid)")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS dashboard_tariffs_isolation ON tariff_rates")
    op.execute("DROP POLICY IF EXISTS dashboard_sources_isolation ON data_sources")
    op.execute("ALTER TABLE tariff_rates DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE data_sources DISABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tariff_rates DROP CONSTRAINT IF EXISTS uq_tariff_rates_identity")
    op.execute("ALTER TABLE tariff_rates ADD CONSTRAINT uq_tariff_rates_identity UNIQUE (fuel, direction, tariff_code, rate_kind, valid_from)")
    op.execute("ALTER TABLE tariff_rates DROP COLUMN IF EXISTS dashboard_id")
    op.execute("ALTER TABLE data_sources DROP CONSTRAINT IF EXISTS uq_data_sources_dashboard_code")
    op.execute("ALTER TABLE data_sources ADD CONSTRAINT data_sources_code_key UNIQUE (code)")
    op.execute("ALTER TABLE data_sources DROP COLUMN IF EXISTS dashboard_id")
