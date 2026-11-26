"""Add Octopus Mini telemetry catalog and live health.

Revision ID: 0004_octopus_mini_telemetry
Revises: 0003_gas_ingestion
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
revision = "0004_octopus_mini_telemetry"
down_revision = "0003_gas_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

METRICS = [
 ("octopus_mini_consumption_power", "Mini consumption power", "power", "W"),
 ("octopus_mini_export_power", "Mini export power", "power", "W"),
 ("octopus_mini_demand_power", "Mini demand power", "power", "W"),
 ("octopus_mini_consumption_delta", "Mini consumption delta", "energy", "kWh"),
 ("octopus_mini_cost_delta", "Mini cost delta excluding VAT", "diagnostic", "pence"),
 ("octopus_mini_cost_delta_inc_vat", "Mini cost delta including VAT", "diagnostic", "pence"),
]
def upgrade():
    metric = sa.table("metric_definitions", sa.column("code", sa.String), sa.column("display_name", sa.String), sa.column("measurement_kind", sa.String), sa.column("canonical_unit", sa.String), sa.column("value_type", sa.String))
    for code, name, kind, unit in METRICS:
        op.get_bind().execute(postgresql.insert(metric).values(code=code, display_name=name, measurement_kind=kind, canonical_unit=unit, value_type="number").on_conflict_do_update(index_elements=[metric.c.code], set_={"display_name":name,"measurement_kind":kind,"canonical_unit":unit,"value_type":"number"}))
    op.create_table("live_provider_health", sa.Column("source_id", sa.Integer, sa.ForeignKey("data_sources.id", ondelete="CASCADE"), primary_key=True), sa.Column("dataset", sa.String(64), primary_key=True), sa.Column("state", sa.String(16), nullable=False), sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=False), sa.Column("last_success_at", sa.DateTime(timezone=True)), sa.Column("last_sample_at", sa.DateTime(timezone=True)), sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"), sa.Column("reason", sa.String(256)), sa.CheckConstraint("state IN ('syncing', 'healthy', 'failed')", name="ck_live_provider_health_state"), sa.CheckConstraint("consecutive_failures >= 0", name="ck_live_provider_health_failures"))
def downgrade():
    op.drop_table("live_provider_health")
    op.execute("DELETE FROM metric_definitions WHERE code LIKE 'octopus_mini_%'")
