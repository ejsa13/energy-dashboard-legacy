"""Add source-aware energy, tariff, telemetry, and ingestion schema.

Revision ID: 0002_energy_data_schema
Revises: 0001_collector_heartbeat
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_energy_data_schema"
down_revision: str | None = "0001_collector_heartbeat"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SOURCE_ROWS = (
    ("octopus_import", "octopus", "settlement_meter", "Octopus import", True),
    ("octopus_export", "octopus", "settlement_meter", "Octopus export", True),
    ("ecoflow_powerocean", "ecoflow", "inverter", "EcoFlow PowerOcean", False),
    ("octopus_mini", "octopus", "home_area_network", "Octopus Home Mini", False),
)

METRIC_ROWS = (
    ("grid_import_energy", "Grid import", "energy", "kWh", "number"),
    ("grid_export_energy", "Grid export", "energy", "kWh", "number"),
    ("solar_generation_energy", "Solar generation", "energy", "kWh", "number"),
    ("home_consumption_energy", "Home consumption", "energy", "kWh", "number"),
    ("battery_charge_energy", "Battery charge", "energy", "kWh", "number"),
    ("battery_discharge_energy", "Battery discharge", "energy", "kWh", "number"),
    ("solar_power", "Solar power", "power", "W", "number"),
    ("home_power", "Home power", "power", "W", "number"),
    ("grid_power", "Grid power", "power", "W", "number"),
    ("battery_power", "Battery power", "power", "W", "number"),
    ("battery_soc", "Battery state of charge", "ratio", "%", "number"),
)


def upgrade() -> None:
    op.create_table(
        "data_sources",
        sa.Column("id", sa.Integer(), sa.Identity(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("is_settlement_authority", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("provider IN ('octopus', 'ecoflow')", name="ck_data_sources_provider"),
        sa.CheckConstraint("source_type IN ('settlement_meter', 'inverter', 'home_area_network')", name="ck_data_sources_type"),
        sa.CheckConstraint(
            "NOT is_settlement_authority OR (provider = 'octopus' AND source_type = 'settlement_meter')",
            name="ck_data_sources_authority",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_table(
        "metric_definitions",
        sa.Column("code", sa.String(96), nullable=False),
        sa.Column("display_name", sa.String(128), nullable=False),
        sa.Column("measurement_kind", sa.String(32), nullable=False),
        sa.Column("canonical_unit", sa.String(32), nullable=True),
        sa.Column("value_type", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("measurement_kind IN ('energy', 'power', 'ratio', 'status', 'diagnostic')", name="ck_metric_definitions_kind"),
        sa.CheckConstraint("value_type IN ('number', 'text', 'boolean')", name="ck_metric_definitions_value_type"),
        sa.PrimaryKeyConstraint("code"),
    )

    source_table = sa.table(
        "data_sources",
        sa.column("code", sa.String),
        sa.column("provider", sa.String),
        sa.column("source_type", sa.String),
        sa.column("display_name", sa.String),
        sa.column("is_settlement_authority", sa.Boolean),
    )
    metric_table = sa.table(
        "metric_definitions",
        sa.column("code", sa.String),
        sa.column("display_name", sa.String),
        sa.column("measurement_kind", sa.String),
        sa.column("canonical_unit", sa.String),
        sa.column("value_type", sa.String),
    )
    source_insert = postgresql.insert(source_table).values(
        [dict(zip(("code", "provider", "source_type", "display_name", "is_settlement_authority"), row, strict=True)) for row in SOURCE_ROWS]
    )
    op.get_bind().execute(
        source_insert.on_conflict_do_update(
            index_elements=[source_table.c.code],
            set_={
                "provider": source_insert.excluded.provider,
                "source_type": source_insert.excluded.source_type,
                "display_name": source_insert.excluded.display_name,
                "is_settlement_authority": source_insert.excluded.is_settlement_authority,
            },
        )
    )
    metric_insert = postgresql.insert(metric_table).values(
        [dict(zip(("code", "display_name", "measurement_kind", "canonical_unit", "value_type"), row, strict=True)) for row in METRIC_ROWS]
    )
    op.get_bind().execute(
        metric_insert.on_conflict_do_update(
            index_elements=[metric_table.c.code],
            set_={
                "display_name": metric_insert.excluded.display_name,
                "measurement_kind": metric_insert.excluded.measurement_kind,
                "canonical_unit": metric_insert.excluded.canonical_unit,
                "value_type": metric_insert.excluded.value_type,
            },
        )
    )

    op.create_table(
        "energy_intervals",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("metric_code", sa.String(96), nullable=False),
        sa.Column("interval_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("value_kwh", sa.Numeric(20, 6), nullable=False),
        sa.Column("quality", sa.String(16), nullable=False),
        sa.Column("completeness", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("interval_end > interval_start", name="ck_energy_intervals_range"),
        sa.CheckConstraint("value_kwh >= 0", name="ck_energy_intervals_nonnegative"),
        sa.CheckConstraint("quality IN ('settled', 'reported', 'derived', 'estimated')", name="ck_energy_intervals_quality"),
        sa.CheckConstraint("completeness IN ('complete', 'partial')", name="ck_energy_intervals_completeness"),
        sa.ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["metric_code"], ["metric_definitions.code"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "metric_code", "interval_start", "interval_end", name="uq_energy_intervals_identity"),
    )
    op.create_index("ix_energy_intervals_metric_range", "energy_intervals", ["metric_code", "interval_start", "interval_end"])

    op.create_table(
        "tariff_rates",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("tariff_code", sa.String(128), nullable=False),
        sa.Column("rate_kind", sa.String(24), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value_inc_vat_pence", sa.Numeric(14, 6), nullable=False),
        sa.Column("value_exc_vat_pence", sa.Numeric(14, 6), nullable=False),
        sa.Column("origin", sa.String(64), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("direction IN ('import', 'export')", name="ck_tariff_rates_direction"),
        sa.CheckConstraint("rate_kind IN ('unit_rate', 'standing_charge')", name="ck_tariff_rates_kind"),
        sa.CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_tariff_rates_range"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("direction", "tariff_code", "rate_kind", "valid_from", name="uq_tariff_rates_identity"),
    )
    op.create_index("ix_tariff_rates_applicable", "tariff_rates", ["direction", "tariff_code", "valid_from", "valid_to"])

    op.create_table(
        "telemetry_samples",
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("metric_code", sa.String(96), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality", sa.String(16), nullable=False),
        sa.Column("value_number", sa.Numeric(20, 6), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.CheckConstraint("num_nonnulls(value_number, value_text, value_boolean) = 1", name="ck_telemetry_samples_one_value"),
        sa.CheckConstraint("quality IN ('reported', 'derived', 'estimated')", name="ck_telemetry_samples_quality"),
        sa.ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["metric_code"], ["metric_definitions.code"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("sampled_at", "source_id", "metric_code"),
        postgresql_partition_by="RANGE (sampled_at)",
    )
    op.create_index("ix_telemetry_samples_latest", "telemetry_samples", ["source_id", "metric_code", sa.text("sampled_at DESC")])
    op.create_index("ix_telemetry_samples_time_brin", "telemetry_samples", ["sampled_at"], postgresql_using="brin")

    op.execute(
        """
        CREATE FUNCTION ensure_telemetry_partition(p_day date) RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE
            partition_name text := 'telemetry_samples_' || to_char(p_day, 'YYYYMMDD');
        BEGIN
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF telemetry_samples FOR VALUES FROM (%L) TO (%L)',
                partition_name,
                p_day::timestamp AT TIME ZONE 'UTC',
                (p_day + 1)::timestamp AT TIME ZONE 'UTC'
            );
        END;
        $$
        """
    )
    op.execute(
        "SELECT ensure_telemetry_partition(day::date) "
        "FROM generate_series((now() AT TIME ZONE 'UTC')::date - 1, "
        "(now() AT TIME ZONE 'UTC')::date + 7, interval '1 day') AS day"
    )

    op.create_table(
        "telemetry_latest",
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("metric_code", sa.String(96), nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quality", sa.String(16), nullable=False),
        sa.Column("value_number", sa.Numeric(20, 6), nullable=True),
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_boolean", sa.Boolean(), nullable=True),
        sa.CheckConstraint("num_nonnulls(value_number, value_text, value_boolean) = 1", name="ck_telemetry_latest_one_value"),
        sa.CheckConstraint("quality IN ('reported', 'derived', 'estimated')", name="ck_telemetry_latest_quality"),
        sa.ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["metric_code"], ["metric_definitions.code"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("source_id", "metric_code"),
    )
    op.create_table(
        "telemetry_rollups",
        sa.Column("bucket_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution_seconds", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("metric_code", sa.String(96), nullable=False),
        sa.Column("bucket_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("minimum_number", sa.Numeric(20, 6), nullable=True),
        sa.Column("maximum_number", sa.Numeric(20, 6), nullable=True),
        sa.Column("average_number", sa.Numeric(20, 6), nullable=True),
        sa.Column("last_number", sa.Numeric(20, 6), nullable=True),
        sa.Column("last_text", sa.Text(), nullable=True),
        sa.Column("last_boolean", sa.Boolean(), nullable=True),
        sa.Column("sample_count", sa.BigInteger(), nullable=False),
        sa.Column("change_count", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("resolution_seconds IN (60, 3600)", name="ck_telemetry_rollups_resolution"),
        sa.CheckConstraint("bucket_end > bucket_start", name="ck_telemetry_rollups_range"),
        sa.CheckConstraint("num_nonnulls(last_number, last_text, last_boolean) = 1", name="ck_telemetry_rollups_one_last"),
        sa.CheckConstraint("sample_count > 0", name="ck_telemetry_rollups_sample_count"),
        sa.ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["metric_code"], ["metric_definitions.code"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("bucket_start", "resolution_seconds", "source_id", "metric_code"),
    )
    op.create_index("ix_telemetry_rollups_range", "telemetry_rollups", ["resolution_seconds", "metric_code", "bucket_start"])

    op.create_table(
        "ingestion_runs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("dataset", sa.String(64), nullable=False),
        sa.Column("requested_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requested_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("rows_received", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("rows_written", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_code", sa.String(64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.CheckConstraint("requested_end IS NULL OR requested_end > requested_start", name="ck_ingestion_runs_range"),
        sa.CheckConstraint("status IN ('running', 'complete', 'partial', 'failed')", name="ck_ingestion_runs_status"),
        sa.CheckConstraint("rows_received >= 0 AND rows_written >= 0", name="ck_ingestion_runs_counts"),
        sa.ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ingestion_runs_source_dataset", "ingestion_runs", ["source_id", "dataset", sa.text("started_at DESC")])
    op.create_table(
        "sync_checkpoints",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("dataset", sa.String(64), nullable=False),
        sa.Column("watermark", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "dataset", name="uq_sync_checkpoints_identity"),
    )
    op.create_table(
        "coverage_windows",
        sa.Column("id", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("source_id", sa.Integer(), nullable=False),
        sa.Column("dataset", sa.String(64), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("ingestion_run_id", sa.BigInteger(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("window_end > window_start", name="ck_coverage_windows_range"),
        sa.CheckConstraint("status IN ('complete', 'partial', 'failed')", name="ck_coverage_windows_status"),
        sa.ForeignKeyConstraint(["source_id"], ["data_sources.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["ingestion_runs.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "dataset", "window_start", "window_end", name="uq_coverage_windows_identity"),
    )
    op.create_index("ix_coverage_windows_range", "coverage_windows", ["source_id", "dataset", "window_start", "window_end"])


def downgrade() -> None:
    op.drop_index("ix_coverage_windows_range", table_name="coverage_windows")
    op.drop_table("coverage_windows")
    op.drop_table("sync_checkpoints")
    op.drop_index("ix_ingestion_runs_source_dataset", table_name="ingestion_runs")
    op.drop_table("ingestion_runs")
    op.drop_index("ix_telemetry_rollups_range", table_name="telemetry_rollups")
    op.drop_table("telemetry_rollups")
    op.drop_table("telemetry_latest")
    op.execute("DROP FUNCTION ensure_telemetry_partition(date)")
    op.drop_index("ix_telemetry_samples_time_brin", table_name="telemetry_samples")
    op.drop_index("ix_telemetry_samples_latest", table_name="telemetry_samples")
    op.drop_table("telemetry_samples")
    op.drop_index("ix_tariff_rates_applicable", table_name="tariff_rates")
    op.drop_table("tariff_rates")
    op.drop_index("ix_energy_intervals_metric_range", table_name="energy_intervals")
    op.drop_table("energy_intervals")
    op.drop_table("metric_definitions")
    op.drop_table("data_sources")
