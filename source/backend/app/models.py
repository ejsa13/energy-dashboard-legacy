"""SQLAlchemy 2 mappings for persisted energy and telemetry data."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class DataSource(Base):
    __tablename__ = "data_sources"
    __table_args__ = (
        UniqueConstraint("dashboard_id", "code", name="uq_data_sources_dashboard_code"),
        CheckConstraint("provider IN ('octopus', 'ecoflow')", name="ck_data_sources_provider"),
        CheckConstraint(
            "source_type IN ('settlement_meter', 'gas_meter', 'inverter', 'home_area_network')",
            name="ck_data_sources_type",
        ),
        CheckConstraint(
            "NOT is_settlement_authority OR (provider = 'octopus' AND source_type IN ('settlement_meter', 'gas_meter'))",
            name="ck_data_sources_authority",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    dashboard_id: Mapped[str | None] = mapped_column(ForeignKey("dashboard_accounts.id", ondelete="CASCADE"), index=True)
    code: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(32))
    source_type: Mapped[str] = mapped_column(String(32))
    display_name: Mapped[str] = mapped_column(String(128))
    is_settlement_authority: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"
    __table_args__ = (
        CheckConstraint(
            "measurement_kind IN ('energy', 'power', 'ratio', 'status', 'diagnostic')",
            name="ck_metric_definitions_kind",
        ),
        CheckConstraint("value_type IN ('number', 'text', 'boolean')", name="ck_metric_definitions_value_type"),
    )

    code: Mapped[str] = mapped_column(String(96), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128))
    measurement_kind: Mapped[str] = mapped_column(String(32))
    canonical_unit: Mapped[str | None] = mapped_column(String(32))
    value_type: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class EnergyInterval(Base):
    __tablename__ = "energy_intervals"
    __table_args__ = (
        CheckConstraint("interval_end > interval_start", name="ck_energy_intervals_range"),
        CheckConstraint("value_kwh >= 0", name="ck_energy_intervals_nonnegative"),
        CheckConstraint(
            "quality IN ('settled', 'reported', 'derived', 'estimated')",
            name="ck_energy_intervals_quality",
        ),
        CheckConstraint("completeness IN ('complete', 'partial')", name="ck_energy_intervals_completeness"),
        UniqueConstraint("source_id", "metric_code", "interval_start", "interval_end", name="uq_energy_intervals_identity"),
        Index("ix_energy_intervals_metric_range", "metric_code", "interval_start", "interval_end"),
        Index("ix_energy_intervals_source_metric_range", "source_id", "metric_code", "interval_start", "interval_end"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    metric_code: Mapped[str] = mapped_column(String(96), ForeignKey("metric_definitions.code", ondelete="RESTRICT"))
    interval_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    interval_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    value_kwh: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    quality: Mapped[str] = mapped_column(String(16))
    completeness: Mapped[str] = mapped_column(String(16))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class TariffRate(Base):
    __tablename__ = "tariff_rates"
    __table_args__ = (
        CheckConstraint("direction IN ('import', 'export')", name="ck_tariff_rates_direction"),
        CheckConstraint("rate_kind IN ('unit_rate', 'standing_charge')", name="ck_tariff_rates_kind"),
        CheckConstraint("valid_to IS NULL OR valid_to > valid_from", name="ck_tariff_rates_range"),
        CheckConstraint("fuel IN ('electricity', 'gas')", name="ck_tariff_rates_fuel"),
        UniqueConstraint("dashboard_id", "fuel", "direction", "tariff_code", "rate_kind", "valid_from", name="uq_tariff_rates_identity"),
        Index("ix_tariff_rates_applicable", "fuel", "direction", "tariff_code", "valid_from", "valid_to"),
        Index("ix_tariff_rates_lookup", "fuel", "direction", "rate_kind", "valid_from", "valid_to"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    dashboard_id: Mapped[str | None] = mapped_column(ForeignKey("dashboard_accounts.id", ondelete="CASCADE"), index=True)
    fuel: Mapped[str] = mapped_column(String(16), server_default=text("'electricity'"))
    direction: Mapped[str] = mapped_column(String(8))
    tariff_code: Mapped[str] = mapped_column(String(128))
    rate_kind: Mapped[str] = mapped_column(String(24))
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    value_inc_vat_pence: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    value_exc_vat_pence: Mapped[Decimal] = mapped_column(Numeric(14, 6))
    origin: Mapped[str] = mapped_column(String(64))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


_ONE_TYPED_VALUE = "num_nonnulls(value_number, value_text, value_boolean) = 1"


class TelemetrySample(Base):
    __tablename__ = "telemetry_samples"
    __table_args__ = (
        CheckConstraint(_ONE_TYPED_VALUE, name="ck_telemetry_samples_one_value"),
        CheckConstraint("quality IN ('reported', 'derived', 'estimated')", name="ck_telemetry_samples_quality"),
        Index("ix_telemetry_samples_latest", "source_id", "metric_code", text("sampled_at DESC")),
        Index("ix_telemetry_samples_time_brin", "sampled_at", postgresql_using="brin"),
        {"postgresql_partition_by": "RANGE (sampled_at)"},
    )

    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"), primary_key=True)
    metric_code: Mapped[str] = mapped_column(String(96), ForeignKey("metric_definitions.code", ondelete="RESTRICT"), primary_key=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    quality: Mapped[str] = mapped_column(String(16))
    value_number: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    value_text: Mapped[str | None] = mapped_column(Text)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean)


class TelemetryLatest(Base):
    __tablename__ = "telemetry_latest"
    __table_args__ = (
        CheckConstraint(_ONE_TYPED_VALUE, name="ck_telemetry_latest_one_value"),
        CheckConstraint("quality IN ('reported', 'derived', 'estimated')", name="ck_telemetry_latest_quality"),
    )

    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), primary_key=True)
    metric_code: Mapped[str] = mapped_column(String(96), ForeignKey("metric_definitions.code", ondelete="CASCADE"), primary_key=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    quality: Mapped[str] = mapped_column(String(16))
    value_number: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    value_text: Mapped[str | None] = mapped_column(Text)
    value_boolean: Mapped[bool | None] = mapped_column(Boolean)


class TelemetryRollup(Base):
    __tablename__ = "telemetry_rollups"
    __table_args__ = (
        CheckConstraint("resolution_seconds IN (60, 3600)", name="ck_telemetry_rollups_resolution"),
        CheckConstraint("bucket_end > bucket_start", name="ck_telemetry_rollups_range"),
        CheckConstraint("num_nonnulls(last_number, last_text, last_boolean) = 1", name="ck_telemetry_rollups_one_last"),
        CheckConstraint("sample_count > 0", name="ck_telemetry_rollups_sample_count"),
        Index("ix_telemetry_rollups_range", "resolution_seconds", "metric_code", "bucket_start"),
    )

    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    resolution_seconds: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), primary_key=True)
    metric_code: Mapped[str] = mapped_column(String(96), ForeignKey("metric_definitions.code", ondelete="CASCADE"), primary_key=True)
    bucket_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    minimum_number: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    maximum_number: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    average_number: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    last_number: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    last_text: Mapped[str | None] = mapped_column(Text)
    last_boolean: Mapped[bool | None] = mapped_column(Boolean)
    sample_count: Mapped[int] = mapped_column(BigInteger)
    change_count: Mapped[int] = mapped_column(BigInteger, default=0)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (
        CheckConstraint("requested_end IS NULL OR requested_end > requested_start", name="ck_ingestion_runs_range"),
        CheckConstraint("status IN ('running', 'complete', 'partial', 'failed')", name="ck_ingestion_runs_status"),
        CheckConstraint("rows_received >= 0 AND rows_written >= 0", name="ck_ingestion_runs_counts"),
        Index("ix_ingestion_runs_source_dataset", "source_id", "dataset", text("started_at DESC")),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="RESTRICT"))
    dataset: Mapped[str] = mapped_column(String(64))
    requested_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    rows_received: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    rows_written: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_detail: Mapped[str | None] = mapped_column(Text)


class SyncCheckpoint(Base):
    __tablename__ = "sync_checkpoints"
    __table_args__ = (UniqueConstraint("source_id", "dataset", name="uq_sync_checkpoints_identity"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    dataset: Mapped[str] = mapped_column(String(64))
    watermark: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cursor: Mapped[str | None] = mapped_column(Text)
    last_success_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))


class CoverageWindow(Base):
    __tablename__ = "coverage_windows"
    __table_args__ = (
        CheckConstraint("window_end > window_start", name="ck_coverage_windows_range"),
        CheckConstraint("status IN ('complete', 'partial', 'failed')", name="ck_coverage_windows_status"),
        UniqueConstraint("source_id", "dataset", "window_start", "window_end", name="uq_coverage_windows_identity"),
        Index("ix_coverage_windows_range", "source_id", "dataset", "window_start", "window_end"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"))
    dataset: Mapped[str] = mapped_column(String(64))
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16))
    ingestion_run_id: Mapped[int | None] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="SET NULL"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text("now()"))

class LiveProviderHealth(Base):
    __tablename__ = "live_provider_health"
    __table_args__ = (
        CheckConstraint("state IN ('syncing', 'healthy', 'failed')", name="ck_live_provider_health_state"),
        CheckConstraint("consecutive_failures >= 0", name="ck_live_provider_health_failures"),
    )
    source_id: Mapped[int] = mapped_column(ForeignKey("data_sources.id", ondelete="CASCADE"), primary_key=True)
    dataset: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    last_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sample_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str | None] = mapped_column(String(256))
    transport: Mapped[str | None] = mapped_column(String(32))
    connection_state: Mapped[str | None] = mapped_column(String(32))
