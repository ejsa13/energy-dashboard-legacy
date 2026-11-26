"""Add Octopus gas source, metric, and tariff fuel separation.

Revision ID: 0003_gas_ingestion
Revises: 0002_energy_data_schema
"""
from collections.abc import Sequence
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_gas_ingestion"
down_revision: str | None = "0002_energy_data_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    op.drop_constraint("ck_data_sources_type", "data_sources", type_="check")
    op.drop_constraint("ck_data_sources_authority", "data_sources", type_="check")
    op.create_check_constraint("ck_data_sources_type", "data_sources", "source_type IN ('settlement_meter', 'gas_meter', 'inverter', 'home_area_network')")
    op.create_check_constraint("ck_data_sources_authority", "data_sources", "NOT is_settlement_authority OR (provider = 'octopus' AND source_type IN ('settlement_meter', 'gas_meter'))")
    source = sa.table("data_sources", sa.column("code", sa.String), sa.column("provider", sa.String), sa.column("source_type", sa.String), sa.column("display_name", sa.String), sa.column("is_settlement_authority", sa.Boolean))
    op.get_bind().execute(postgresql.insert(source).values(code="octopus_gas", provider="octopus", source_type="gas_meter", display_name="Octopus gas", is_settlement_authority=True).on_conflict_do_update(index_elements=[source.c.code], set_={"provider":"octopus", "source_type":"gas_meter", "display_name":"Octopus gas", "is_settlement_authority":True}))
    metric = sa.table("metric_definitions", sa.column("code", sa.String), sa.column("display_name", sa.String), sa.column("measurement_kind", sa.String), sa.column("canonical_unit", sa.String), sa.column("value_type", sa.String))
    op.get_bind().execute(postgresql.insert(metric).values(code="gas_consumption_kwh", display_name="Gas consumption", measurement_kind="energy", canonical_unit="kWh", value_type="number").on_conflict_do_update(index_elements=[metric.c.code], set_={"display_name":"Gas consumption", "measurement_kind":"energy", "canonical_unit":"kWh", "value_type":"number"}))
    op.add_column("tariff_rates", sa.Column("fuel", sa.String(16), server_default=sa.text("'electricity'"), nullable=False))
    op.create_check_constraint("ck_tariff_rates_fuel", "tariff_rates", "fuel IN ('electricity', 'gas')")
    op.drop_constraint("uq_tariff_rates_identity", "tariff_rates", type_="unique")
    op.create_unique_constraint("uq_tariff_rates_identity", "tariff_rates", ["fuel", "direction", "tariff_code", "rate_kind", "valid_from"])
    op.drop_index("ix_tariff_rates_applicable", table_name="tariff_rates")
    op.create_index("ix_tariff_rates_applicable", "tariff_rates", ["fuel", "direction", "tariff_code", "valid_from", "valid_to"])

def downgrade() -> None:
    op.drop_index("ix_tariff_rates_applicable", table_name="tariff_rates")
    op.drop_constraint("uq_tariff_rates_identity", "tariff_rates", type_="unique")
    op.create_unique_constraint("uq_tariff_rates_identity", "tariff_rates", ["direction", "tariff_code", "rate_kind", "valid_from"])
    op.drop_constraint("ck_tariff_rates_fuel", "tariff_rates", type_="check")
    op.drop_column("tariff_rates", "fuel")
    op.execute("DELETE FROM metric_definitions WHERE code = 'gas_consumption_kwh'")
    op.execute("DELETE FROM data_sources WHERE code = 'octopus_gas'")
    op.drop_constraint("ck_data_sources_authority", "data_sources", type_="check")
    op.drop_constraint("ck_data_sources_type", "data_sources", type_="check")
    op.create_check_constraint("ck_data_sources_type", "data_sources", "source_type IN ('settlement_meter', 'inverter', 'home_area_network')")
    op.create_check_constraint("ck_data_sources_authority", "data_sources", "NOT is_settlement_authority OR (provider = 'octopus' AND source_type = 'settlement_meter')")
