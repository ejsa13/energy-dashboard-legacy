"""Typed persistence interfaces for normalized energy data.

Provider adapters should translate their fields into these inputs. Successful raw
provider payloads and credentials have no persistence interface here by design.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, TypeAlias

from sqlalchemy import Engine, Select, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.catalog import METRIC_SEEDS, SOURCE_SEEDS
from app.models import (
    CoverageWindow,
    DataSource,
    EnergyInterval,
    IngestionRun,
    MetricDefinition,
    SyncCheckpoint,
    TariffRate,
    TelemetryLatest,
    TelemetrySample,
    LiveProviderHealth,
)

EnergyQuality: TypeAlias = Literal["settled", "reported", "derived", "estimated"]
Completeness: TypeAlias = Literal["complete", "partial"]
TelemetryQuality: TypeAlias = Literal["reported", "derived", "estimated"]
RunStatus: TypeAlias = Literal["complete", "partial", "failed"]

_SENSITIVE_PATTERN = re.compile(
    r"(?i)(api[-_ ]?key|authorization|bearer|password|secret|access[-_ ]?key|refresh[-_ ]?token|raw[-_ ]?payload)"
)


def _require_aware(value: datetime | None, name: str, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _require_range(start: datetime, end: datetime, prefix: str = "interval") -> None:
    _require_aware(start, f"{prefix}_start")
    _require_aware(end, f"{prefix}_end")
    if end <= start:
        raise ValueError(f"{prefix}_end must be after {prefix}_start")


def _reject_sensitive(value: str | None, name: str) -> None:
    if value and _SENSITIVE_PATTERN.search(value):
        raise ValueError(f"{name} must not contain credentials or raw provider payloads")


@dataclass(frozen=True, slots=True)
class EnergyIntervalInput:
    source_code: str
    metric_code: str
    interval_start: datetime
    interval_end: datetime
    value_kwh: Decimal
    quality: EnergyQuality
    completeness: Completeness = "complete"
    observed_at: datetime | None = None

    def validate(self) -> None:
        _require_range(self.interval_start, self.interval_end)
        _require_aware(self.observed_at, "observed_at", optional=True)
        if self.value_kwh < 0:
            raise ValueError("directional energy must not be negative")
        if self.quality not in {"settled", "reported", "derived", "estimated"}:
            raise ValueError("unknown energy quality")
        if self.completeness not in {"complete", "partial"}:
            raise ValueError("unknown interval completeness")


@dataclass(frozen=True, slots=True)
class TariffRateInput:
    direction: Literal["import", "export"]
    tariff_code: str
    rate_kind: Literal["unit_rate", "standing_charge"]
    valid_from: datetime
    valid_to: datetime | None
    value_inc_vat_pence: Decimal
    value_exc_vat_pence: Decimal
    origin: str
    fuel: Literal["electricity", "gas"] = "electricity"

    def validate(self) -> None:
        _require_aware(self.valid_from, "valid_from")
        _require_aware(self.valid_to, "valid_to", optional=True)
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be after valid_from")
        if self.fuel not in {"electricity", "gas"} or self.direction not in {"import", "export"}:
            raise ValueError("unknown tariff direction")
        if self.rate_kind not in {"unit_rate", "standing_charge"}:
            raise ValueError("unknown tariff rate kind")
        if not self.tariff_code or not self.origin:
            raise ValueError("tariff_code and origin are required")


@dataclass(frozen=True, slots=True)
class TelemetrySampleInput:
    source_code: str
    metric_code: str
    sampled_at: datetime
    received_at: datetime
    quality: TelemetryQuality = "reported"
    value_number: Decimal | None = None
    value_text: str | None = None
    value_boolean: bool | None = None

    def validate(self) -> None:
        _require_aware(self.sampled_at, "sampled_at")
        _require_aware(self.received_at, "received_at")
        if sum(value is not None for value in (self.value_number, self.value_text, self.value_boolean)) != 1:
            raise ValueError("telemetry requires exactly one typed value")
        if self.quality not in {"reported", "derived", "estimated"}:
            raise ValueError("unknown telemetry quality")


class CatalogRepository:
    def __init__(self, engine: Engine):
        self.engine = engine

    def seed(self, dashboard_id: str | None = None) -> None:
        """Insert or refresh stable catalog entries without changing identities."""
        with self.engine.begin() as connection:
            if dashboard_id is None:
                # Legacy/unscoped data is retained only for archived local
                # installs. NULLs do not collide in PostgreSQL unique keys, so
                # seed it explicitly instead of creating duplicate sources.
                existing = set(connection.execute(select(DataSource.code).where(DataSource.dashboard_id.is_(None))).scalars())
                missing = [source for source in SOURCE_SEEDS if source["code"] not in existing]
                if missing:
                    connection.execute(insert(DataSource).values([{**source, "dashboard_id": None} for source in missing]))
            else:
                sources = [{**source, "dashboard_id": dashboard_id} for source in SOURCE_SEEDS]
                source_statement = insert(DataSource).values(sources)
                connection.execute(
                    source_statement.on_conflict_do_update(
                        index_elements=[DataSource.dashboard_id, DataSource.code],
                        set_={
                            "provider": source_statement.excluded.provider,
                            "source_type": source_statement.excluded.source_type,
                            "display_name": source_statement.excluded.display_name,
                            "is_settlement_authority": source_statement.excluded.is_settlement_authority,
                        },
                    )
                )
            metric_statement = insert(MetricDefinition).values(list(METRIC_SEEDS))
            connection.execute(metric_statement.on_conflict_do_update(index_elements=[MetricDefinition.code], set_={
                "display_name": metric_statement.excluded.display_name,
                "measurement_kind": metric_statement.excluded.measurement_kind,
                "canonical_unit": metric_statement.excluded.canonical_unit,
                "value_type": metric_statement.excluded.value_type,
            }))

    def register_metrics(self, metrics: list[dict[str, str | None]]) -> None:
        """Idempotently register decoded provider metrics; never accepts raw fields."""
        if not metrics:
            return
        allowed_kinds = {"energy", "power", "ratio", "status", "diagnostic"}
        allowed_types = {"number", "text", "boolean"}
        for metric in metrics:
            code = metric.get("code") or ""
            if not re.fullmatch(r"ecoflow_[a-z0-9_]{1,80}", code):
                raise ValueError("EcoFlow metric code is invalid")
            if metric.get("measurement_kind") not in allowed_kinds or metric.get("value_type") not in allowed_types:
                raise ValueError("EcoFlow metric definition is invalid")
        with self.engine.begin() as connection:
            statement = insert(MetricDefinition).values(metrics)
            connection.execute(statement.on_conflict_do_update(index_elements=[MetricDefinition.code], set_={
                "display_name": statement.excluded.display_name,
                "measurement_kind": statement.excluded.measurement_kind,
                "canonical_unit": statement.excluded.canonical_unit,
                "value_type": statement.excluded.value_type,
            }))
            metric_statement = insert(MetricDefinition).values(list(METRIC_SEEDS))
            connection.execute(
                metric_statement.on_conflict_do_update(
                    index_elements=[MetricDefinition.code],
                    set_={
                        "display_name": metric_statement.excluded.display_name,
                        "measurement_kind": metric_statement.excluded.measurement_kind,
                        "canonical_unit": metric_statement.excluded.canonical_unit,
                        "value_type": metric_statement.excluded.value_type,
                    },
                )
            )


def _catalog_maps(
    connection, source_codes: set[str], metric_codes: set[str], dashboard_id: str | None = None
) -> tuple[dict[str, int], dict[str, tuple[str, str]]]:
    # SQLAlchemy binds a Python string as VARCHAR. PostgreSQL does not compare
    # that implicitly with the UUID dashboard column, so normalize scoped
    # repository callers before constructing the source lookup.
    from uuid import UUID
    dashboard_uuid = UUID(str(dashboard_id)) if dashboard_id is not None else None
    sources = dict(
        connection.execute(select(DataSource.code, DataSource.id).where(DataSource.code.in_(source_codes), DataSource.dashboard_id == dashboard_uuid)).all()
    )
    metrics = {
        code: (value_type, measurement_kind)
        for code, value_type, measurement_kind in connection.execute(
            select(MetricDefinition.code, MetricDefinition.value_type, MetricDefinition.measurement_kind).where(
                MetricDefinition.code.in_(metric_codes)
            )
        ).all()
    }
    missing_sources = source_codes - sources.keys()
    missing_metrics = metric_codes - metrics.keys()
    if missing_sources:
        raise ValueError(f"unknown source code(s): {', '.join(sorted(missing_sources))}")
    if missing_metrics:
        raise ValueError(f"unknown metric code(s): {', '.join(sorted(missing_metrics))}")
    return sources, metrics


def _dashboard_uuid(dashboard_id: str | None):
    from uuid import UUID
    return UUID(str(dashboard_id)) if dashboard_id is not None else None


class EnergyIntervalRepository:
    def __init__(self, engine: Engine, dashboard_id: str | None = None):
        self.engine, self.dashboard_id = engine, dashboard_id

    def upsert_batch(self, items: list[EnergyIntervalInput]) -> int:
        if not items:
            return 0
        for item in items:
            item.validate()
        with self.engine.begin() as connection:
            sources, metrics = _catalog_maps(
                connection,
                {item.source_code for item in items},
                {item.metric_code for item in items},
                self.dashboard_id,
            )
            non_energy = [code for code in {item.metric_code for item in items} if metrics[code] != ("number", "energy")]
            if non_energy:
                raise ValueError(f"energy intervals require numeric energy metrics: {', '.join(sorted(non_energy))}")
            invalid_settlement = [
                item
                for item in items
                if item.quality == "settled"
                and (item.source_code, item.metric_code)
                not in {
                    ("octopus_import", "grid_import_energy"),
                    ("octopus_export", "grid_export_energy"),
                    ("octopus_gas", "gas_consumption_kwh"),
                }
            ]
            if invalid_settlement:
                raise ValueError("settled quality is only valid for matching Octopus grid import/export metrics")
            rows = [
                {
                    "source_id": sources[item.source_code],
                    "metric_code": item.metric_code,
                    "interval_start": item.interval_start,
                    "interval_end": item.interval_end,
                    "value_kwh": item.value_kwh,
                    "quality": item.quality,
                    "completeness": item.completeness,
                    "observed_at": item.observed_at,
                }
                for item in items
            ]
            statement = insert(EnergyInterval).values(rows)
            result = connection.execute(
                statement.on_conflict_do_update(
                    constraint="uq_energy_intervals_identity",
                    set_={
                        "value_kwh": statement.excluded.value_kwh,
                        "quality": statement.excluded.quality,
                        "completeness": statement.excluded.completeness,
                        "observed_at": statement.excluded.observed_at,
                        "updated_at": func.now(),
                    },
                ).returning(EnergyInterval.id)
            )
            return len(result.all())

    def range_query(
        self,
        *,
        start: datetime,
        end: datetime,
        source_codes: list[str] | None = None,
        metric_codes: list[str] | None = None,
    ) -> list[EnergyInterval]:
        _require_range(start, end, "query")
        statement: Select[tuple[EnergyInterval]] = (
            select(EnergyInterval)
            .join(DataSource)
            .where(EnergyInterval.interval_start < end, EnergyInterval.interval_end > start, DataSource.dashboard_id == _dashboard_uuid(self.dashboard_id))
            .order_by(EnergyInterval.interval_start, DataSource.code, EnergyInterval.metric_code)
        )
        if source_codes:
            statement = statement.where(DataSource.code.in_(source_codes))
        if metric_codes:
            statement = statement.where(EnergyInterval.metric_code.in_(metric_codes))
        with Session(self.engine) as session:
            return list(session.scalars(statement))


class TariffRateRepository:
    def __init__(self, engine: Engine, dashboard_id: str | None = None):
        self.engine, self.dashboard_id = engine, dashboard_id

    def upsert_batch(self, items: list[TariffRateInput]) -> int:
        if not items:
            return 0
        for item in items:
            item.validate()
        rows = [
            {
                "dashboard_id": self.dashboard_id,
                "direction": item.direction,
                "fuel": item.fuel,
                "tariff_code": item.tariff_code,
                "rate_kind": item.rate_kind,
                "valid_from": item.valid_from,
                "valid_to": item.valid_to,
                "value_inc_vat_pence": item.value_inc_vat_pence,
                "value_exc_vat_pence": item.value_exc_vat_pence,
                "origin": item.origin,
            }
            for item in items
        ]
        with self.engine.begin() as connection:
            # PostgreSQL treats NULL values as distinct in a unique constraint.
            # Keep the archived, unscoped repository mode idempotent; new
            # dashboard writes always provide a non-null dashboard id.
            if self.dashboard_id is None:
                for item in items:
                    connection.execute(TariffRate.__table__.delete().where(
                        TariffRate.dashboard_id.is_(None), TariffRate.fuel == item.fuel,
                        TariffRate.direction == item.direction, TariffRate.tariff_code == item.tariff_code,
                        TariffRate.rate_kind == item.rate_kind, TariffRate.valid_from == item.valid_from,
                    ))
            statement = insert(TariffRate).values(rows)
            result = connection.execute(
                statement.on_conflict_do_update(
                    constraint="uq_tariff_rates_identity",
                    set_={
                        "valid_to": statement.excluded.valid_to,
                        "value_inc_vat_pence": statement.excluded.value_inc_vat_pence,
                        "value_exc_vat_pence": statement.excluded.value_exc_vat_pence,
                        "origin": statement.excluded.origin,
                        "updated_at": func.now(),
                    },
                ).returning(TariffRate.id)
            )
            return len(result.all())

    def applicable(
        self,
        *,
        fuel: Literal["electricity", "gas"] = "electricity",
        direction: Literal["import", "export"],
        tariff_code: str,
        rate_kind: Literal["unit_rate", "standing_charge"],
        at: datetime,
    ) -> TariffRate | None:
        _require_aware(at, "at")
        statement = (
            select(TariffRate)
            .where(
                TariffRate.dashboard_id == _dashboard_uuid(self.dashboard_id),
                TariffRate.fuel == fuel,
                TariffRate.direction == direction,
                TariffRate.tariff_code == tariff_code,
                TariffRate.rate_kind == rate_kind,
                TariffRate.valid_from <= at,
                (TariffRate.valid_to.is_(None) | (TariffRate.valid_to > at)),
            )
            .order_by(TariffRate.valid_from.desc())
            .limit(1)
        )
        with Session(self.engine) as session:
            return session.scalars(statement).one_or_none()


class TelemetryRepository:
    def __init__(self, engine: Engine, dashboard_id: str | None = None):
        self.engine, self.dashboard_id = engine, dashboard_id

    def insert_batch(self, items: list[TelemetrySampleInput], *, deduplicate_states: bool = True) -> int:
        if not items:
            return 0
        for item in items:
            item.validate()
        with self.engine.begin() as connection:
            sources, metric_catalog = _catalog_maps(
                connection,
                {item.source_code for item in items},
                {item.metric_code for item in items},
                self.dashboard_id,
            )
            for day in sorted({item.sampled_at.astimezone(timezone.utc).date() for item in items}):
                connection.exec_driver_sql("SELECT ensure_telemetry_partition(%s)", (day,))

            latest_rows: dict[tuple[int, str], tuple[str | None, bool | None]] = {}
            if deduplicate_states:
                latest_rows = {
                    (row.source_id, row.metric_code): (row.value_text, row.value_boolean)
                    for row in connection.execute(
                        select(
                            TelemetryLatest.source_id,
                            TelemetryLatest.metric_code,
                            TelemetryLatest.value_text,
                            TelemetryLatest.value_boolean,
                        ).where(
                            TelemetryLatest.source_id.in_(set(sources.values())),
                            TelemetryLatest.metric_code.in_(set(metric_catalog)),
                        )
                    )
                }

            rows: list[dict[str, object]] = []
            for item in items:
                expected = metric_catalog[item.metric_code][0]
                actual = "number" if item.value_number is not None else "text" if item.value_text is not None else "boolean"
                if actual != expected:
                    raise ValueError(f"metric {item.metric_code} requires a {expected} value")
                source_id = sources[item.source_code]
                prior = latest_rows.get((source_id, item.metric_code))
                if deduplicate_states and actual != "number" and prior is not None:
                    same_value = (
                        prior[0] == item.value_text
                        if actual == "text"
                        else prior[1] == item.value_boolean
                    )
                    if same_value:
                        continue
                rows.append(
                    {
                        "sampled_at": item.sampled_at,
                        "source_id": source_id,
                        "metric_code": item.metric_code,
                        "received_at": item.received_at,
                        "quality": item.quality,
                        "value_number": item.value_number,
                        "value_text": item.value_text,
                        "value_boolean": item.value_boolean,
                    }
                )
            if not rows:
                return 0

            sample_statement = insert(TelemetrySample).values(rows).on_conflict_do_nothing(
                index_elements=[TelemetrySample.sampled_at, TelemetrySample.source_id, TelemetrySample.metric_code]
            ).returning(TelemetrySample.sampled_at)
            inserted_count = len(connection.execute(sample_statement).all())

            latest_by_metric: dict[tuple[int, str], dict[str, object]] = {}
            for row in rows:
                key = (int(row["source_id"]), str(row["metric_code"]))
                current = latest_by_metric.get(key)
                if current is None or row["sampled_at"] >= current["sampled_at"]:
                    latest_by_metric[key] = row
            latest_statement = insert(TelemetryLatest).values(list(latest_by_metric.values()))
            connection.execute(
                latest_statement.on_conflict_do_update(
                    index_elements=[TelemetryLatest.source_id, TelemetryLatest.metric_code],
                    set_={
                        "sampled_at": latest_statement.excluded.sampled_at,
                        "received_at": latest_statement.excluded.received_at,
                        "quality": latest_statement.excluded.quality,
                        "value_number": latest_statement.excluded.value_number,
                        "value_text": latest_statement.excluded.value_text,
                        "value_boolean": latest_statement.excluded.value_boolean,
                    },
                    where=latest_statement.excluded.sampled_at >= TelemetryLatest.sampled_at,
                )
            )
            return inserted_count

    def latest(self, source_code: str | None = None) -> list[TelemetryLatest]:
        statement = select(TelemetryLatest).join(DataSource).where(DataSource.dashboard_id == _dashboard_uuid(self.dashboard_id)).order_by(DataSource.code, TelemetryLatest.metric_code)
        if source_code:
            statement = statement.where(DataSource.code == source_code)
        with Session(self.engine) as session:
            return list(session.scalars(statement))


class IngestionRepository:
    def __init__(self, engine: Engine, dashboard_id: str | None = None):
        self.engine, self.dashboard_id = engine, dashboard_id

    def start_run(
        self,
        source_code: str,
        dataset: str,
        requested_start: datetime | None = None,
        requested_end: datetime | None = None,
    ) -> int:
        _reject_sensitive(dataset, "dataset")
        if requested_start is not None:
            _require_aware(requested_start, "requested_start")
        if requested_end is not None:
            _require_aware(requested_end, "requested_end")
        if requested_start is not None and requested_end is not None and requested_end <= requested_start:
            raise ValueError("requested_end must be after requested_start")
        with self.engine.begin() as connection:
            sources, _ = _catalog_maps(connection, {source_code}, set(), self.dashboard_id)
            return connection.execute(
                insert(IngestionRun)
                .values(
                    source_id=sources[source_code],
                    dataset=dataset,
                    requested_start=requested_start,
                    requested_end=requested_end,
                    status="running",
                )
                .returning(IngestionRun.id)
            ).scalar_one()

    def finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        rows_received: int,
        rows_written: int,
        failure_code: str | None = None,
        failure_detail: str | None = None,
    ) -> None:
        if status not in {"complete", "partial", "failed"}:
            raise ValueError("terminal ingestion status is required")
        if rows_received < 0 or rows_written < 0:
            raise ValueError("row counts must not be negative")
        _reject_sensitive(failure_code, "failure_code")
        _reject_sensitive(failure_detail, "failure_detail")
        with self.engine.begin() as connection:
            result = connection.execute(
                IngestionRun.__table__.update()
                .where(IngestionRun.id == run_id, IngestionRun.status == "running")
                .values(
                    status=status,
                    rows_received=rows_received,
                    rows_written=rows_written,
                    completed_at=func.now(),
                    failure_code=failure_code,
                    failure_detail=failure_detail,
                )
            )
            if result.rowcount != 1:
                raise ValueError("unknown or already completed ingestion run")

    def record_coverage(
        self,
        *,
        source_code: str,
        dataset: str,
        start: datetime,
        end: datetime,
        status: RunStatus,
        run_id: int | None = None,
    ) -> None:
        _require_range(start, end, "window")
        _reject_sensitive(dataset, "dataset")
        if status not in {"complete", "partial", "failed"}:
            raise ValueError("unknown coverage status")
        with self.engine.begin() as connection:
            sources, _ = _catalog_maps(connection, {source_code}, set(), self.dashboard_id)
            statement = insert(CoverageWindow).values(
                source_id=sources[source_code],
                dataset=dataset,
                window_start=start,
                window_end=end,
                status=status,
                ingestion_run_id=run_id,
            )
            connection.execute(
                statement.on_conflict_do_update(
                    constraint="uq_coverage_windows_identity",
                    set_={"status": statement.excluded.status, "ingestion_run_id": statement.excluded.ingestion_run_id, "recorded_at": func.now()},
                )
            )

    def update_checkpoint(
        self,
        *,
        source_code: str,
        dataset: str,
        watermark: datetime | None,
        cursor: str | None,
        succeeded_at: datetime,
    ) -> None:
        _require_aware(watermark, "watermark", optional=True)
        _require_aware(succeeded_at, "succeeded_at")
        _reject_sensitive(dataset, "dataset")
        _reject_sensitive(cursor, "cursor")
        with self.engine.begin() as connection:
            sources, _ = _catalog_maps(connection, {source_code}, set(), self.dashboard_id)
            statement = insert(SyncCheckpoint).values(
                source_id=sources[source_code],
                dataset=dataset,
                watermark=watermark,
                cursor=cursor,
                last_success_at=succeeded_at,
            )
            connection.execute(
                statement.on_conflict_do_update(
                    constraint="uq_sync_checkpoints_identity",
                    set_={
                        "watermark": statement.excluded.watermark,
                        "cursor": statement.excluded.cursor,
                        "last_success_at": statement.excluded.last_success_at,
                        "updated_at": func.now(),
                    },
                )
            )

    def checkpoint(self, *, source_code: str, dataset: str) -> datetime | None:
        _reject_sensitive(dataset, "dataset")
        statement = (
            select(SyncCheckpoint.watermark)
            .join(DataSource)
            .where(DataSource.code == source_code, DataSource.dashboard_id == _dashboard_uuid(self.dashboard_id), SyncCheckpoint.dataset == dataset)
        )
        with self.engine.connect() as connection:
            return connection.execute(statement).scalar_one_or_none()

    def earliest_complete_coverage(self, *, source_code: str, dataset: str) -> datetime | None:
        _reject_sensitive(dataset, "dataset")
        statement = (
            select(func.min(CoverageWindow.window_start))
            .join(DataSource)
            .where(
                DataSource.code == source_code, DataSource.dashboard_id == _dashboard_uuid(self.dashboard_id),
                CoverageWindow.dataset == dataset,
                CoverageWindow.status == "complete",
            )
        )
        with self.engine.connect() as connection:
            return connection.execute(statement).scalar_one()

    def latest_run(self, *, source_code: str) -> IngestionRun | None:
        statement = (
            select(IngestionRun)
            .join(DataSource)
            .where(DataSource.code == source_code, DataSource.dashboard_id == _dashboard_uuid(self.dashboard_id))
            .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc())
            .limit(1)
        )
        with Session(self.engine) as session:
            return session.scalars(statement).one_or_none()


class LiveProviderHealthRepository:
    def __init__(self, engine: Engine, dashboard_id: str | None = None): self.engine, self.dashboard_id = engine, dashboard_id
    def update(self, *, source_code: str, dataset: str, state: str, attempted_at: datetime, sample_at: datetime | None = None, reason: str | None = None, transport: str | None = None, connection_state: str | None = None) -> None:
        if state not in {"syncing", "healthy", "failed"}: raise ValueError("unknown live provider state")
        _require_aware(attempted_at, "attempted_at"); _require_aware(sample_at, "sample_at", optional=True); _reject_sensitive(reason, "reason")
        if transport not in {None, "mqtt", "http_fallback"}: raise ValueError("unknown live transport")
        if connection_state not in {None, "connecting", "connected", "stale", "disconnected"}: raise ValueError("unknown live connection state")
        with self.engine.begin() as c:
            sources, _ = _catalog_maps(c, {source_code}, set(), self.dashboard_id)
            old = Session(bind=c).scalars(select(LiveProviderHealth).where(LiveProviderHealth.source_id == sources[source_code], LiveProviderHealth.dataset == dataset)).one_or_none()
            failures = 0 if state == "healthy" else (old.consecutive_failures + 1 if old else 1)
            stmt = insert(LiveProviderHealth).values(source_id=sources[source_code],dataset=dataset,state=state,last_attempt_at=attempted_at,last_success_at=attempted_at if state=="healthy" else None,last_sample_at=sample_at,consecutive_failures=failures,reason=reason,transport=transport,connection_state=connection_state)
            c.execute(stmt.on_conflict_do_update(index_elements=[LiveProviderHealth.source_id,LiveProviderHealth.dataset],set_={"state":state,"last_attempt_at":attempted_at,"last_success_at": attempted_at if state=="healthy" else (old.last_success_at if old else None),"last_sample_at": sample_at if state=="healthy" else (old.last_sample_at if old else None),"consecutive_failures":failures,"reason":reason,"transport":transport if transport is not None else (old.transport if old else None),"connection_state":connection_state if connection_state is not None else (old.connection_state if old else None)}))
    def get(self, source_code: str, dataset: str):
        with Session(self.engine) as s:
            dashboard_id = _dashboard_uuid(self.dashboard_id)
            return s.scalars(select(LiveProviderHealth).join(DataSource).where(DataSource.code==source_code,DataSource.dashboard_id == dashboard_id,LiveProviderHealth.dataset==dataset)).one_or_none()
