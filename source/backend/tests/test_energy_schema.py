from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.catalog import METRIC_SEEDS, SOURCE_SEEDS
from app.models import (
    CoverageWindow,
    DataSource,
    IngestionRun,
    MetricDefinition,
    SyncCheckpoint,
    TariffRate,
    TelemetryRollup,
    TelemetrySample,
)
from app.repositories import (
    CatalogRepository,
    EnergyIntervalInput,
    EnergyIntervalRepository,
    IngestionRepository,
    TariffRateInput,
    TariffRateRepository,
    TelemetryRepository,
    TelemetrySampleInput,
)
from app.telemetry_maintenance import TelemetryMaintenance

UTC = timezone.utc


def test_migration_seeds_are_stable_and_revision_is_head(postgres_engine):
    CatalogRepository(postgres_engine).seed()
    CatalogRepository(postgres_engine).seed()
    with postgres_engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0016_dashboard_selection"
        assert connection.execute(select(func.count()).select_from(DataSource)).scalar_one() == len(SOURCE_SEEDS)
        assert connection.execute(select(func.count()).select_from(MetricDefinition)).scalar_one() == len(METRIC_SEEDS)
        authority = dict(connection.execute(select(DataSource.code, DataSource.is_settlement_authority)).all())
    assert authority == {
        "octopus_import": True,
        "octopus_export": True,
        "octopus_gas": True,
        "ecoflow_powerocean": False,
        "octopus_mini": False,
    }


def test_energy_upsert_preserves_zero_absence_and_parallel_sources(postgres_engine):
    repository = EnergyIntervalRepository(postgres_engine)
    start = datetime(2026, 3, 29, 0, 30, tzinfo=UTC)  # UK DST transition remains an ordinary UTC interval.
    end = start + timedelta(minutes=30)
    repository.upsert_batch(
        [
            EnergyIntervalInput("octopus_import", "grid_import_energy", start, end, Decimal("0"), "settled"),
            EnergyIntervalInput("ecoflow_powerocean", "grid_import_energy", start, end, Decimal("0.123456"), "reported"),
        ]
    )
    repository.upsert_batch(
        [EnergyIntervalInput("octopus_import", "grid_import_energy", start, end, Decimal("0.500001"), "settled")]
    )
    rows = repository.range_query(start=start, end=end)
    assert len(rows) == 2
    assert sorted(row.value_kwh for row in rows) == [Decimal("0.123456"), Decimal("0.500001")]
    assert not repository.range_query(start=end, end=end + timedelta(minutes=30))


def test_energy_validation_rejects_bad_inputs_and_unknown_catalog(postgres_engine):
    repository = EnergyIntervalRepository(postgres_engine)
    aware = datetime.now(UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        repository.upsert_batch(
            [EnergyIntervalInput("octopus_import", "grid_import_energy", aware.replace(tzinfo=None), aware, Decimal("1"), "settled")]
        )
    with pytest.raises(ValueError, match="after"):
        repository.upsert_batch(
            [EnergyIntervalInput("octopus_import", "grid_import_energy", aware, aware, Decimal("1"), "settled")]
        )
    with pytest.raises(ValueError, match="negative"):
        repository.upsert_batch(
            [EnergyIntervalInput("octopus_import", "grid_import_energy", aware, aware + timedelta(minutes=30), Decimal("-1"), "settled")]
        )
    with pytest.raises(ValueError, match="unknown source"):
        repository.upsert_batch(
            [EnergyIntervalInput("missing", "grid_import_energy", aware, aware + timedelta(minutes=30), Decimal("1"), "settled")]
        )


def test_tariffs_keep_decimal_negative_and_zero_values(postgres_engine):
    repository = TariffRateRepository(postgres_engine)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    repository.upsert_batch(
        [
            TariffRateInput("import", "AGILE", "unit_rate", start, start + timedelta(minutes=30), Decimal("-1.234567"), Decimal("-1.175778"), "octopus_api"),
            TariffRateInput("export", "OUTGOING", "unit_rate", start, None, Decimal("0"), Decimal("0"), "configured_fallback"),
        ]
    )
    agile = repository.applicable(direction="import", tariff_code="AGILE", rate_kind="unit_rate", at=start)
    outgoing = repository.applicable(direction="export", tariff_code="OUTGOING", rate_kind="unit_rate", at=start)
    assert agile is not None and agile.value_inc_vat_pence == Decimal("-1.234567")
    assert outgoing is not None and outgoing.value_inc_vat_pence == Decimal("0.000000")
    repository.upsert_batch(
        [TariffRateInput("import", "AGILE", "unit_rate", start, start + timedelta(minutes=30), Decimal("2.000001"), Decimal("1.904763"), "octopus_api")]
    )
    with postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(TariffRate)).scalar_one() == 2


def test_telemetry_history_latest_and_rollups(postgres_engine):
    repository = TelemetryRepository(postgres_engine)
    base = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=5)
    items = [
        TelemetrySampleInput("ecoflow_powerocean", "solar_power", base + timedelta(seconds=5), base + timedelta(seconds=6), value_number=Decimal("100.000001")),
        TelemetrySampleInput("ecoflow_powerocean", "solar_power", base + timedelta(seconds=35), base + timedelta(seconds=36), value_number=Decimal("300.000003")),
        TelemetrySampleInput("ecoflow_powerocean", "solar_power", base + timedelta(minutes=1, seconds=5), base + timedelta(minutes=1, seconds=6), value_number=Decimal("200")),
    ]
    assert repository.insert_batch(items) == 3
    assert repository.insert_batch(items) == 0
    latest = repository.latest("ecoflow_powerocean")
    assert len(latest) == 1 and latest[0].value_number == Decimal("200.000000")
    TelemetryMaintenance(postgres_engine).run(base + timedelta(hours=1))
    with postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(TelemetrySample)).scalar_one() == 3
        minute = connection.execute(
            select(
                TelemetryRollup.minimum_number,
                TelemetryRollup.maximum_number,
                TelemetryRollup.average_number,
                TelemetryRollup.last_number,
            ).where(
                TelemetryRollup.resolution_seconds == 60,
                TelemetryRollup.bucket_start == base,
            )
        ).first()
        rollup_count = connection.execute(select(func.count()).select_from(TelemetryRollup)).scalar_one()
    assert minute is not None
    assert minute.minimum_number == Decimal("100.000001")
    assert minute.maximum_number == Decimal("300.000003")
    assert minute.average_number == Decimal("200.000002")
    assert minute.last_number == Decimal("300.000003")
    TelemetryMaintenance(postgres_engine).run(base + timedelta(hours=1))
    with postgres_engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(TelemetryRollup)).scalar_one() == rollup_count


def test_telemetry_rejects_invalid_typed_values(postgres_engine):
    repository = TelemetryRepository(postgres_engine)
    now = datetime.now(UTC)
    with pytest.raises(ValueError, match="exactly one"):
        repository.insert_batch(
            [TelemetrySampleInput("ecoflow_powerocean", "solar_power", now, now, value_number=Decimal("1"), value_text="one")]
        )
    with pytest.raises(ValueError, match="requires a number"):
        repository.insert_batch(
            [TelemetrySampleInput("ecoflow_powerocean", "solar_power", now, now, value_text="one")]
        )


def test_categorical_rollup_tracks_last_value_and_changes(postgres_engine):
    with postgres_engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO metric_definitions (code, display_name, measurement_kind, canonical_unit, value_type)
                VALUES ('powerocean_state', 'PowerOcean state', 'status', NULL, 'text')
                ON CONFLICT (code) DO NOTHING
                """
            )
        )
    base = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=5)
    TelemetryRepository(postgres_engine).insert_batch(
        [
            TelemetrySampleInput("ecoflow_powerocean", "powerocean_state", base + timedelta(seconds=2), base + timedelta(seconds=2), value_text="idle"),
            TelemetrySampleInput("ecoflow_powerocean", "powerocean_state", base + timedelta(seconds=20), base + timedelta(seconds=20), value_text="charging"),
        ]
    )
    TelemetryMaintenance(postgres_engine).run(base + timedelta(hours=1))
    with postgres_engine.connect() as connection:
        last_text, change_count = connection.execute(
            select(TelemetryRollup.last_text, TelemetryRollup.change_count).where(
                TelemetryRollup.resolution_seconds == 60,
                TelemetryRollup.metric_code == "powerocean_state",
                TelemetryRollup.bucket_start == base,
            )
        ).one()
    assert last_text == "charging"
    assert change_count == 1


def test_retention_rolls_up_before_dropping_expired_partition(postgres_engine):
    now = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
    old_time = now - timedelta(days=31)
    repository = TelemetryRepository(postgres_engine)
    repository.insert_batch(
        [TelemetrySampleInput("ecoflow_powerocean", "home_power", old_time, old_time, value_number=Decimal("42"))]
    )
    maintenance = TelemetryMaintenance(postgres_engine, retention_days=30)
    assert maintenance.run(now)
    stats = maintenance.storage_stats()
    assert stats.raw_row_count == 0
    assert stats.oldest_sample is None
    assert stats.rollup_row_count == 2


def test_ingestion_lifecycle_coverage_and_checkpoint(postgres_engine):
    repository = IngestionRepository(postgres_engine)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    run_id = repository.start_run("octopus_import", "consumption", start, end)
    repository.finish_run(run_id, status="partial", rows_received=48, rows_written=47, failure_detail="one interval unavailable")
    repository.record_coverage(source_code="octopus_import", dataset="consumption", start=start, end=end, status="partial", run_id=run_id)
    repository.update_checkpoint(source_code="octopus_import", dataset="consumption", watermark=end, cursor="page-2", succeeded_at=end)
    with postgres_engine.connect() as connection:
        assert connection.execute(select(IngestionRun.status)).scalar_one() == "partial"
        assert connection.execute(select(CoverageWindow.status)).scalar_one() == "partial"
        assert connection.execute(select(SyncCheckpoint.watermark)).scalar_one() == end
    with pytest.raises(ValueError, match="credentials"):
        repository.finish_run(
            repository.start_run("octopus_import", "retry"),
            status="failed",
            rows_received=0,
            rows_written=0,
            failure_detail="Authorization: Bearer secret-value",
        )
