"""Collector job for Octopus import settlement history and tariff rates."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

from sqlalchemy import text
from zoneinfo import ZoneInfo

from app.database import get_engine
from app.octopus import OctopusApiError, OctopusClient, OctopusContext, OctopusGasContext, parse_api_datetime
from app.repositories import CatalogRepository, EnergyIntervalInput, EnergyIntervalRepository, IngestionRepository, TariffRateInput, TariffRateRepository, TelemetryRepository, TelemetrySampleInput

logger = logging.getLogger("collector.octopus")
LONDON = ZoneInfo("Europe/London")
HALF_HOUR = timedelta(minutes=30)
# Octopus gas consumption is supplied in cubic metres.  This standard
# correction-factor/calorific-value conversion (1.02264 × 39.9 ÷ 3.6)
# matches the supplier's billed kWh totals.
GAS_M3_TO_KWH = Decimal("11.34")
RECONCILIATION_DAYS = 14
# Recheck older partial settlement windows throughout the day while avoiding a
# five-minute request loop when Octopus has not published the missing data.
PARTIAL_RETRY_INTERVAL = timedelta(hours=6)


class OctopusImportConfigurationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class OctopusImportConfig:
    api_key: str
    account_number: str

    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> "OctopusImportConfig":
        # An explicitly supplied empty mapping is useful in tests and must not
        # silently fall back to the collector process environment.
        values = env if env is not None else os.environ
        required = (
            "OCTOPUS_API_KEY",
            "OCTOPUS_ACCOUNT_NUMBER",
        )
        missing = [name for name in required if not values.get(name, "").strip()]
        if missing:
            raise OctopusImportConfigurationError("Missing required Octopus collector configuration")
        return cls(
            api_key=values["OCTOPUS_API_KEY"].strip(),
            account_number=values["OCTOPUS_ACCOUNT_NUMBER"].strip(),
        )


def floor_half_hour(value: datetime) -> datetime:
    value = value.astimezone(timezone.utc)
    return value.replace(minute=value.minute - (value.minute % 30), second=0, microsecond=0)


def ceil_half_hour(value: datetime) -> datetime:
    floored = floor_half_hour(value)
    return floored if floored == value.astimezone(timezone.utc) else floored + HALF_HOUR


def month_ranges(start: datetime, end: datetime) -> Iterable[tuple[datetime, datetime]]:
    """Produce UTC ranges bounded by Europe/London calendar months."""
    local = start.astimezone(LONDON)
    cursor = datetime.combine(date(local.year, local.month, 1), datetime_time.min, LONDON)
    while cursor.astimezone(timezone.utc) < end:
        following = datetime(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1, tzinfo=LONDON)
        range_start = max(cursor.astimezone(timezone.utc), start)
        range_end = min(following.astimezone(timezone.utc), end)
        if range_end > range_start:
            yield range_start, range_end
        cursor = following


def day_ranges(start: datetime, end: datetime) -> Iterable[tuple[datetime, datetime]]:
    """Produce UTC ranges bounded by Europe/London calendar days.

    The Octopus consumption endpoint can page a longer request.  Fetching one
    local day at a time avoids an incomplete page chain while still allowing
    the normal monthly checkpoint and coverage model to be retained.
    """
    local = start.astimezone(LONDON)
    cursor = datetime.combine(local.date(), datetime_time.min, LONDON)
    while cursor.astimezone(timezone.utc) < end:
        following = cursor + timedelta(days=1)
        range_start = max(cursor.astimezone(timezone.utc), start)
        range_end = min(following.astimezone(timezone.utc), end)
        if range_end > range_start:
            yield range_start, range_end
        cursor = following


def fetch_consumption_days(fetch, start, end):
    """Bound provider I/O concurrency; database writes stay on the scoped caller."""
    ranges=list(day_ranges(start,end))
    with ThreadPoolExecutor(max_workers=4,thread_name_prefix="octopus-history") as executor:
        pages=executor.map(lambda window:fetch(*window),ranges)
        return [row for page in pages for row in page]


def expected_slots(start: datetime, end: datetime) -> set[tuple[datetime, datetime]]:
    slots: set[tuple[datetime, datetime]] = set()
    cursor = start
    while cursor < end:
        slots.add((cursor, cursor + HALF_HOUR))
        cursor += HALF_HOUR
    return slots


def overlapping_agreements(agreements: Iterable[dict[str, Any]], start: datetime, end: datetime) -> list[dict[str, Any]]:
    result = []
    for agreement in agreements:
        try:
            valid_from = parse_api_datetime(agreement.get("valid_from"))
            valid_to = parse_api_datetime(agreement["valid_to"]) if agreement.get("valid_to") else None
        except (KeyError, OctopusApiError):
            continue
        if valid_to is not None and valid_to <= valid_from:
            continue
        if valid_from < end and (valid_to is None or start < valid_to) and isinstance(agreement.get("tariff_code"), str):
            result.append(agreement)
    return sorted(result, key=lambda item: str(item["valid_from"]))


def clipped_rate_inputs(
    rows: Iterable[dict[str, Any]], *, tariff_code: str, rate_kind: str, start: datetime, end: datetime, agreement_start: datetime, agreement_end: datetime | None, fuel: str = "electricity", direction: str = "import"
) -> list[TariffRateInput]:
    inputs: list[TariffRateInput] = []
    effective_agreement_end = agreement_end or end
    for row in rows:
        if row.get("payment_method") not in {None, "DIRECT_DEBIT"}:
            continue
        try:
            row_start = parse_api_datetime(row.get("valid_from"))
            row_end = parse_api_datetime(row["valid_to"]) if row.get("valid_to") else None
            # Keep a rate from the beginning of the customer agreement when
            # it was already active before this sync window. Otherwise each
            # rolling sync creates an artificial unpriced gap at its start.
            clipped_start = max(row_start, agreement_start, start)
            clipped_end_candidates = [value for value in (row_end, effective_agreement_end, end) if value is not None]
            clipped_end = min(clipped_end_candidates) if clipped_end_candidates else None
            if clipped_end is not None and clipped_end <= clipped_start:
                continue
            inc_vat = Decimal(str(row["value_inc_vat"]))
            exc_vat = Decimal(str(row["value_exc_vat"]))
        except (KeyError, InvalidOperation, OctopusApiError) as exc:
            raise OctopusApiError("Octopus API returned an invalid tariff rate") from exc
        inputs.append(
            TariffRateInput(
                direction=direction,  # type: ignore[arg-type]
                tariff_code=tariff_code,
                rate_kind=rate_kind,  # type: ignore[arg-type]
                valid_from=clipped_start,
                valid_to=clipped_end,
                value_inc_vat_pence=inc_vat,
                value_exc_vat_pence=exc_vat,
                origin="octopus_api",
                fuel=fuel,  # type: ignore[arg-type]
            )
        )
    return inputs


class OctopusHistoricalImporter:
    def __init__(
        self,
        *,
        client_factory: Callable[[str], OctopusClient] = OctopusClient,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], None] = time.sleep,
        env: dict[str, str] | None = None,
        dashboard_id: str | None = None,
    ):
        self.client_factory = client_factory
        self.now = now
        self.sleep = sleep
        self.env, self.dashboard_id = env, dashboard_id
        self.ingestion = IngestionRepository(get_engine(), dashboard_id)
        self.energy = EnergyIntervalRepository(get_engine(), dashboard_id)
        self.tariffs = TariffRateRepository(get_engine(), dashboard_id)
        self.catalog = CatalogRepository(get_engine())
        self.telemetry = TelemetryRepository(get_engine(), dashboard_id)

    def run(self) -> None:
        now = floor_half_hour(self.now())
        try:
            self.catalog.seed(self.dashboard_id)
            config = OctopusImportConfig.from_environment(self.env)
            client = self.client_factory(config.api_key)
            context = self._retry(lambda: client.resolve_import_context(account_number=config.account_number))
        except (OctopusImportConfigurationError, OctopusApiError) as exc:
            self._record_startup_failure(now, exc)
            logger.warning("Octopus historical import could not start: %s", type(exc).__name__)
            return

        logger.info("Octopus historical import authenticated; checking electricity, export and gas coverage.")

        self._store_identifier("octopus_import", "ecoflow_octopus_import_mpan", context.mpan, now)
        export_context = None
        gas_context = None
        if hasattr(client, "resolve_export_context"):
            try:
                export_context = self._retry(lambda: client.resolve_export_context(account_number=config.account_number))
                self._store_identifier("octopus_export", "ecoflow_octopus_export_mpan", export_context.mpan, now)
            except OctopusApiError:
                pass
        try:
            gas_context = self._retry(lambda: client.resolve_gas_context(account_number=config.account_number))
            self._store_identifier("octopus_gas", "ecoflow_octopus_gas_mprn", gas_context.mprn, now)
        except OctopusApiError as exc:
            self._record_gas_startup_failure(now, exc)
        # Export credit feeds the fiscal-year savings tracker, so backfill it
        # before the larger import and gas historical jobs.
        if export_context is not None:
            logger.info("Octopus export history and tariffs are being synchronised.")
            self._sync_export_tariffs(client, export_context, now)
            self._sync_export_consumption(client, export_context, now)
        if gas_context is not None:
            logger.info("Octopus gas history and tariffs are being synchronised.")
            self._sync_gas_tariffs(client, gas_context, now)
            self._sync_gas_consumption(client, gas_context, now)
        logger.info("Octopus electricity consumption and tariffs are being synchronised.")
        self._sync_tariffs(client, context.tariff_agreements, context.earliest_available_at, config, now)
        self._sync_consumption(client, context, config, now)
        logger.info("Octopus historical import completed.")

    def _store_identifier(self, source: str, metric: str, value: str, now: datetime) -> None:
        self.catalog.register_metrics([{"code": metric, "display_name": metric.replace("_", " ").upper(), "measurement_kind": "diagnostic", "canonical_unit": None, "value_type": "text"}])
        self.telemetry.insert_batch([TelemetrySampleInput(source, metric, now, now, value_text=value)])

    def _retryable_coverage(self, *, source_code: str, dataset: str, end: datetime) -> list[tuple[datetime, datetime]]:
        """Return every recorded incomplete range for a full catch-up pass."""
        with get_engine().connect() as connection:
            rows = connection.execute(text("""SELECT cw.window_start, cw.window_end FROM coverage_windows cw
              JOIN data_sources ds ON ds.id=cw.source_id
              WHERE ds.dashboard_id IS NOT DISTINCT FROM CAST(:dashboard AS uuid) AND ds.code=:source AND cw.dataset=:dataset AND cw.status IN ('partial', 'failed')
                AND cw.window_end <= :end
                AND (cw.status='failed' OR cw.window_end > :recent OR cw.recorded_at < :retry_before)
                AND NOT EXISTS (SELECT 1 FROM coverage_windows newer WHERE newer.source_id=cw.source_id AND newer.dataset=cw.dataset
                  AND newer.id<>cw.id AND newer.window_start<=cw.window_start AND newer.window_end>=cw.window_end
                  AND newer.recorded_at>=cw.recorded_at AND newer.status IN ('complete', 'partial'))
              ORDER BY cw.window_start"""), {"source": source_code, "dataset": dataset, "end": end, "dashboard":self.dashboard_id,"recent":end-timedelta(days=RECONCILIATION_DAYS),"retry_before":self.now()-PARTIAL_RETRY_INTERVAL}).all()
        return [(row.window_start, row.window_end) for row in rows]

    @staticmethod
    def _merge_ranges(*groups: Iterable[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
        """Coalesce overlapping retries so each interval is fetched once."""
        ranges = sorted({(start, end) for group in groups for start, end in group if end > start})
        merged: list[tuple[datetime, datetime]] = []
        for start, end in ranges:
            # Adjacent historical months remain separate jobs; only truly
            # overlapping coverage windows describe the same data.
            if not merged or start >= merged[-1][1]:
                merged.append((start, end))
                continue
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        return merged

    def _unattempted_ranges(self, source, dataset, start, end):
        """A watermark cannot prove that earlier calendar windows were attempted."""
        with get_engine().connect() as connection:
            rows=connection.execute(text("""SELECT cw.window_start,cw.window_end FROM coverage_windows cw
                JOIN data_sources ds ON ds.id=cw.source_id
                WHERE ds.dashboard_id IS NOT DISTINCT FROM CAST(:dashboard AS uuid)
                AND ds.code=:source AND cw.dataset=:dataset AND cw.window_end>:start AND cw.window_start<:end
                ORDER BY cw.window_start"""),{"dashboard":self.dashboard_id,"source":source,"dataset":dataset,"start":start,"end":end}).all()
        cursor=start
        gaps=[]
        for a,b in rows:
            if a>cursor: gaps.extend(month_ranges(cursor,min(a,end)))
            cursor=max(cursor,b)
        if cursor<end: gaps.extend(month_ranges(cursor,end))
        return gaps

    def _tariff_backfill(self, ranges, earliest, end, fuel, direction):
        with get_engine().connect() as connection:
            first = connection.execute(text("""SELECT min(valid_from) FROM tariff_rates
                WHERE dashboard_id IS NOT DISTINCT FROM CAST(:dashboard AS uuid)
                AND origin='octopus_api' AND fuel=:fuel AND direction=:direction AND rate_kind='unit_rate'"""),
                {"dashboard":self.dashboard_id,"fuel":fuel,"direction":direction}).scalar_one_or_none()
        until = min(first or end,end)
        return self._merge_ranges(month_ranges(earliest,until),ranges) if earliest < until else ranges

    def _ranges(self, *, dataset: str, earliest_available_at: datetime, end: datetime) -> tuple[str, list[tuple[datetime, datetime]]]:
        watermark = self.ingestion.checkpoint(source_code="octopus_import", dataset=dataset)
        reconciliation_start = max(earliest_available_at, floor_half_hour(end - timedelta(days=RECONCILIATION_DAYS)))
        # Coverage records remain the proof of completeness.  The watermark is only a
        # resume cursor, so partial/empty early account months never block later data.
        retry = self._merge_ranges(self._retryable_coverage(source_code="octopus_import", dataset=dataset, end=end), self._unattempted_ranges("octopus_import",dataset,earliest_available_at,end))
        if watermark is None:
            return "backfill", self._merge_ranges(month_ranges(earliest_available_at, end), retry)
        if watermark < reconciliation_start:
            return "backfill", self._merge_ranges(month_ranges(max(earliest_available_at, watermark), end), retry)
        return "sync", self._merge_ranges([(reconciliation_start, end)] if end > reconciliation_start else [], retry)

    def _gas_ranges(self, *, dataset: str, earliest_available_at: datetime, end: datetime) -> tuple[str, list[tuple[datetime, datetime]]]:
        watermark = self.ingestion.checkpoint(source_code="octopus_gas", dataset=dataset)
        reconciliation_start = max(earliest_available_at, floor_half_hour(end - timedelta(days=RECONCILIATION_DAYS)))
        retry = self._merge_ranges(self._retryable_coverage(source_code="octopus_gas", dataset=dataset, end=end), self._unattempted_ranges("octopus_gas",dataset,earliest_available_at,end))
        if watermark is None: return "backfill", self._merge_ranges(month_ranges(earliest_available_at, end), retry)
        if watermark < reconciliation_start: return "backfill", self._merge_ranges(month_ranges(max(earliest_available_at, watermark), end), retry)
        return "sync", self._merge_ranges([(reconciliation_start, end)] if end > reconciliation_start else [], retry)

    def _latest_gas_serial(self, client: OctopusClient, context: OctopusGasContext, end: datetime) -> str:
        probe_start = floor_half_hour(end - timedelta(days=14))
        newest: dict[str, datetime] = {}
        for serial in context.meter_serials:
            rows = self._retry(lambda serial=serial: client.gas_consumption(mprn=context.mprn, meter_serial=serial, start=probe_start, end=end))
            ends = [parse_api_datetime(row.get("interval_end")) for row in rows if isinstance(row, dict) and row.get("interval_end")]
            if ends:
                newest[serial] = max(ends)
        if not newest:
            raise OctopusApiError("No recent gas readings were available for discovered meters")
        latest = max(newest.values())
        winners = [serial for serial, observed_end in newest.items() if observed_end == latest]
        if len(winners) != 1:
            raise OctopusApiError("Could not uniquely select the latest gas meter")
        return winners[0]

    def _sync_gas_consumption(self, client: OctopusClient, context: OctopusGasContext, end: datetime) -> None:
        serial = self._latest_gas_serial(client, context, end)
        earliest = ceil_half_hour(context.earliest_available_at)
        mode, ranges = self._gas_ranges(dataset="gas_consumption", earliest_available_at=earliest, end=end)
        for start, range_end in ranges:
            run_id = self.ingestion.start_run("octopus_gas", f"gas_consumption_{mode}", start, range_end)
            try:
                rows = fetch_consumption_days(lambda a,b:self._retry(lambda:client.gas_consumption(
                    mprn=context.mprn,meter_serial=serial,start=a,end=b)),start,range_end)
                items, slots = self._gas_consumption_inputs(rows, start, range_end)
                written = self.energy.upsert_batch(items)
                state = "complete" if expected_slots(start, range_end).issubset(slots) else "partial"
                self.ingestion.finish_run(run_id, status=state, rows_received=len(rows), rows_written=written)
                self.ingestion.record_coverage(source_code="octopus_gas", dataset="gas_consumption", start=start, end=range_end, status=state, run_id=run_id)
                self.ingestion.update_checkpoint(source_code="octopus_gas", dataset="gas_consumption", watermark=range_end, cursor=None, succeeded_at=self.now())
            except OctopusApiError as exc:
                self._fail_gas_run(run_id, start, range_end, "gas_consumption", exc); break

    def _sync_gas_tariffs(self, client: OctopusClient, context: OctopusGasContext, end: datetime) -> None:
        mode, ranges = self._gas_ranges(dataset="gas_tariffs", earliest_available_at=context.earliest_available_at, end=end)
        ranges = self._tariff_backfill(ranges,context.earliest_available_at,end,"gas","import")
        for start, range_end in ranges:
            run_id = self.ingestion.start_run("octopus_gas", f"gas_tariffs_{mode}", start, range_end)
            try:
                inputs: list[TariffRateInput] = []; received = 0
                for agreement in overlapping_agreements(context.tariff_agreements, start, range_end):
                    code = str(agreement["tariff_code"]); astart = parse_api_datetime(agreement["valid_from"]); aend = parse_api_datetime(agreement["valid_to"]) if agreement.get("valid_to") else None
                    for kind, fetch in (("unit_rate", client.gas_unit_rates), ("standing_charge", client.gas_standing_charges)):
                        rows = self._retry(lambda fetch=fetch: fetch(tariff_code=code, start=start, end=range_end)); received += len(rows)
                        inputs.extend(clipped_rate_inputs(rows, tariff_code=code, rate_kind=kind, start=start, end=range_end, agreement_start=astart, agreement_end=aend, fuel="gas"))
                unique = {(item.fuel, item.direction, item.tariff_code, item.rate_kind, item.valid_from): item for item in inputs}
                written = self.tariffs.upsert_batch(list(unique.values()))
                self.ingestion.finish_run(run_id, status="complete", rows_received=len(unique), rows_written=written)
                self.ingestion.record_coverage(source_code="octopus_gas", dataset="gas_tariffs", start=start, end=range_end, status="complete", run_id=run_id)
                self.ingestion.update_checkpoint(source_code="octopus_gas", dataset="gas_tariffs", watermark=range_end, cursor=None, succeeded_at=self.now())
            except OctopusApiError as exc:
                self._fail_gas_run(run_id, start, range_end, "gas_tariffs", exc); break

    def _sync_export_tariffs(self, client: OctopusClient, context: OctopusContext, end: datetime) -> None:
        """Import outgoing rates so exported energy can be valued as a credit."""
        mode, ranges = self._export_tariff_ranges(end=end)
        ranges = self._tariff_backfill(ranges,context.earliest_available_at,end,"electricity","export")
        for start, range_end in ranges:
            run_id = self.ingestion.start_run("octopus_export", f"export_tariffs_{mode}", start, range_end)
            try:
                inputs: list[TariffRateInput] = []
                for agreement in overlapping_agreements(context.tariff_agreements, start, range_end):
                    code = str(agreement["tariff_code"])
                    agreement_start = parse_api_datetime(agreement["valid_from"])
                    agreement_end = parse_api_datetime(agreement["valid_to"]) if agreement.get("valid_to") else None
                    # Include the preceding day so a tariff active at a range
                    # boundary is retained for the first settled interval.
                    rate_start = start - timedelta(days=1)
                    rows = self._retry(lambda code=code, rate_start=rate_start, range_end=range_end: client.unit_rates(tariff_code=code, start=rate_start, end=range_end))
                    inputs.extend(clipped_rate_inputs(rows, tariff_code=code, rate_kind="unit_rate", start=start, end=range_end, agreement_start=agreement_start, agreement_end=agreement_end, direction="export"))
                unique = {(item.fuel, item.direction, item.tariff_code, item.rate_kind, item.valid_from): item for item in inputs}
                written = self.tariffs.upsert_batch(list(unique.values()))
                self.ingestion.finish_run(run_id, status="complete", rows_received=len(unique), rows_written=written)
                self.ingestion.record_coverage(source_code="octopus_export", dataset="export_tariffs", start=start, end=range_end, status="complete", run_id=run_id)
                self.ingestion.update_checkpoint(source_code="octopus_export", dataset="export_tariffs", watermark=range_end, cursor=None, succeeded_at=self.now())
            except OctopusApiError as exc:
                self.ingestion.finish_run(run_id, status="failed", rows_received=0, rows_written=0, failure_code="octopus_api", failure_detail=str(exc))
                break

    def _export_history_start(self, end: datetime) -> datetime:
        """Start outgoing-meter history at the active fiscal year's beginning.

        The account endpoint can expose only the current outgoing agreement,
        even though the meter has settled export readings from earlier in the
        same fiscal year.
        """
        configured = parse_api_datetime((self.env if self.env is not None else os.environ)["ECOFLOW_START_DATE"]).astimezone(LONDON).date()
        local_end = end.astimezone(LONDON).date()
        fiscal_year = local_end.year if local_end.month >= configured.month else local_end.year - 1
        return datetime(fiscal_year, configured.month, 1, tzinfo=LONDON).astimezone(timezone.utc)

    def _export_ranges(self, *, end: datetime) -> tuple[str, list[tuple[datetime, datetime]]]:
        start = self._export_history_start(end)
        watermark = self.ingestion.checkpoint(source_code="octopus_export", dataset="export_consumption")
        retry = self._merge_ranges(self._retryable_coverage(source_code="octopus_export", dataset="export_consumption", end=end), self._unattempted_ranges("octopus_export","export_consumption",start,end))
        reconciliation_start = floor_half_hour(end - timedelta(days=RECONCILIATION_DAYS))
        if watermark is None or watermark < reconciliation_start:
            cursor = max(start, watermark) if watermark is not None else start
            return "backfill", self._merge_ranges(month_ranges(cursor, end), retry)
        return "sync", self._merge_ranges([(reconciliation_start, end)] if end > reconciliation_start else [], retry)

    def _export_tariff_ranges(self, *, end: datetime) -> tuple[str, list[tuple[datetime, datetime]]]:
        start = self._export_history_start(end)
        watermark = self.ingestion.checkpoint(source_code="octopus_export", dataset="export_tariffs")
        retry = self._retryable_coverage(source_code="octopus_export", dataset="export_tariffs", end=end)
        with get_engine().connect() as connection:
            covered_start = connection.execute(text("""SELECT min(cw.window_start) FROM coverage_windows cw
              JOIN data_sources ds ON ds.id=cw.source_id
              WHERE ds.dashboard_id IS NOT DISTINCT FROM CAST(:dashboard AS uuid) AND ds.code='octopus_export' AND cw.dataset='export_tariffs' AND cw.status='complete'"""), {"dashboard":self.dashboard_id}).scalar_one_or_none()
        reconciliation_start = floor_half_hour(end - timedelta(days=RECONCILIATION_DAYS))
        if covered_start is None or covered_start > start or watermark is None or watermark < reconciliation_start:
            cursor = max(start, watermark) if watermark is not None else start
            if covered_start is not None and covered_start > start:
                cursor = start
            return "backfill", self._merge_ranges(month_ranges(cursor, end), retry)
        return "sync", self._merge_ranges([(reconciliation_start, end)] if end > reconciliation_start else [], retry)

    def _sync_export_consumption(self, client: OctopusClient, context: OctopusContext, end: datetime) -> None:
        mode, ranges = self._export_ranges(end=end)
        for range_start, range_end in ranges:
            run_id = self.ingestion.start_run("octopus_export", f"export_consumption_{mode}", range_start, range_end)
            try:
                rows = fetch_consumption_days(lambda a,b:[row for serial in context.meter_serials
                    for row in self._retry(lambda:client.consumption(mpan=context.mpan,meter_serial=serial,start=a,end=b))],range_start,range_end)
                items, slots = self._export_consumption_inputs(rows, range_start, range_end)
                written = self.energy.upsert_batch(items)
                state = "complete" if expected_slots(range_start, range_end).issubset(slots) else "partial"
                self.ingestion.finish_run(run_id, status=state, rows_received=len(rows), rows_written=written)
                self.ingestion.record_coverage(source_code="octopus_export", dataset="export_consumption", start=range_start, end=range_end, status=state, run_id=run_id)
                self.ingestion.update_checkpoint(source_code="octopus_export", dataset="export_consumption", watermark=range_end, cursor=None, succeeded_at=self.now())
            except OctopusApiError as exc:
                self.ingestion.finish_run(run_id, status="failed", rows_received=0, rows_written=0, failure_code="octopus_api", failure_detail=str(exc))
                break

    def _sync_consumption(self, client: OctopusClient, context: OctopusContext, config: OctopusImportConfig, end: datetime) -> None:
        consumption_start = ceil_half_hour(context.earliest_available_at)
        mode, ranges = self._ranges(dataset="consumption", earliest_available_at=consumption_start, end=end)
        for start, range_end in ranges:
            run_id = self.ingestion.start_run("octopus_import", f"consumption_{mode}", start, range_end)
            try:
                raw_rows = fetch_consumption_days(lambda a,b:[row for serial in context.meter_serials
                    for row in self._retry(lambda:client.consumption(mpan=context.mpan,meter_serial=serial,start=a,end=b))],start,range_end)
                items, returned_slots = self._consumption_inputs(raw_rows, start, range_end)
                written = self.energy.upsert_batch(items)
                complete = expected_slots(start, range_end).issubset(returned_slots)
                state = "complete" if complete else "partial"
                self.ingestion.finish_run(run_id, status=state, rows_received=len(raw_rows), rows_written=written)
                self.ingestion.record_coverage(source_code="octopus_import", dataset="consumption", start=start, end=range_end, status=state, run_id=run_id)
                # A checkpoint is a progress hint, not proof of complete coverage.  Empty
                # account-history months must not prevent later settled readings importing.
                self.ingestion.update_checkpoint(source_code="octopus_import", dataset="consumption", watermark=range_end, cursor=None, succeeded_at=self.now())
            except OctopusApiError as exc:
                self._fail_run(run_id, start, range_end, "consumption", exc)
                break

    def _sync_tariffs(self, client: OctopusClient, agreements: Iterable[dict[str, Any]], earliest_available_at: datetime, config: OctopusImportConfig, end: datetime) -> None:
        mode, ranges = self._ranges(dataset="tariffs", earliest_available_at=earliest_available_at, end=end)
        ranges = self._tariff_backfill(ranges,earliest_available_at,end,"electricity","import")
        for start, range_end in ranges:
            run_id = self.ingestion.start_run("octopus_import", f"tariffs_{mode}", start, range_end)
            try:
                applicable = overlapping_agreements(agreements, start, range_end)
                if not applicable:
                    self.ingestion.finish_run(run_id, status="partial", rows_received=0, rows_written=0)
                    self.ingestion.record_coverage(source_code="octopus_import", dataset="tariffs", start=start, end=range_end, status="partial", run_id=run_id)
                    break
                inputs: list[TariffRateInput] = []
                received = 0
                for agreement in applicable:
                    tariff_code = str(agreement["tariff_code"])
                    agreement_start = parse_api_datetime(agreement["valid_from"])
                    agreement_end = parse_api_datetime(agreement["valid_to"]) if agreement.get("valid_to") else None
                    for kind, fetch in (("unit_rate", client.unit_rates), ("standing_charge", client.standing_charges)):
                        rows = self._retry(lambda fetch=fetch: fetch(tariff_code=tariff_code, start=start, end=range_end))
                        received += len(rows)
                        inputs.extend(clipped_rate_inputs(rows, tariff_code=tariff_code, rate_kind=kind, start=start, end=range_end, agreement_start=agreement_start, agreement_end=agreement_end))
                deduplicated = {(item.direction, item.tariff_code, item.rate_kind, item.valid_from): item for item in inputs}
                written = self.tariffs.upsert_batch(list(deduplicated.values()))
                self.ingestion.finish_run(run_id, status="complete", rows_received=len(deduplicated), rows_written=written)
                self.ingestion.record_coverage(source_code="octopus_import", dataset="tariffs", start=start, end=range_end, status="complete", run_id=run_id)
                self.ingestion.update_checkpoint(source_code="octopus_import", dataset="tariffs", watermark=range_end, cursor=None, succeeded_at=self.now())
            except OctopusApiError as exc:
                self._fail_run(run_id, start, range_end, "tariffs", exc)
                break

    @staticmethod
    def _consumption_inputs(rows: Iterable[dict[str, Any]], start: datetime, end: datetime) -> tuple[list[EnergyIntervalInput], set[tuple[datetime, datetime]]]:
        inputs: list[EnergyIntervalInput] = []
        values_by_slot: dict[tuple[datetime, datetime], Decimal] = {}
        for row in rows:
            try:
                interval_start = parse_api_datetime(row.get("interval_start"))
                interval_end = parse_api_datetime(row.get("interval_end"))
                value = Decimal(str(row["consumption"]))
            except (KeyError, InvalidOperation, OctopusApiError) as exc:
                raise OctopusApiError("Octopus API returned an invalid consumption interval") from exc
            if interval_start < start or interval_end > end:
                continue
            if interval_end - interval_start != HALF_HOUR or value < 0:
                raise OctopusApiError("Octopus API returned an invalid consumption interval")
            slot = (interval_start, interval_end)
            existing = values_by_slot.get(slot)
            if existing is not None and existing != value:
                raise OctopusApiError("Octopus API returned conflicting meter intervals")
            values_by_slot[slot] = value
        for (interval_start, interval_end), value in sorted(values_by_slot.items()):
            inputs.append(EnergyIntervalInput("octopus_import", "grid_import_energy", interval_start, interval_end, value, "settled"))
        return inputs, set(values_by_slot)

    @staticmethod
    def _export_consumption_inputs(rows: Iterable[dict[str, Any]], start: datetime, end: datetime) -> tuple[list[EnergyIntervalInput], set[tuple[datetime, datetime]]]:
        inputs: list[EnergyIntervalInput] = []
        values: dict[tuple[datetime, datetime], Decimal] = {}
        for row in rows:
            try:
                a=parse_api_datetime(row.get("interval_start")); b=parse_api_datetime(row.get("interval_end")); value=Decimal(str(row["consumption"]))
            except (KeyError, InvalidOperation, OctopusApiError) as exc: raise OctopusApiError("Octopus API returned an invalid export interval") from exc
            if a < start or b > end or b-a != HALF_HOUR or value < 0: continue
            if (a,b) in values and values[(a,b)] != value: raise OctopusApiError("Octopus API returned conflicting export intervals")
            values[(a,b)] = value
        return [EnergyIntervalInput("octopus_export", "grid_export_energy", a, b, value, "settled") for (a,b),value in sorted(values.items())], set(values)

    @staticmethod
    def _gas_consumption_inputs(rows: Iterable[dict[str, Any]], start: datetime, end: datetime) -> tuple[list[EnergyIntervalInput], set[tuple[datetime, datetime]]]:
        values: dict[tuple[datetime, datetime], Decimal] = {}
        for row in rows:
            try:
                interval_start = parse_api_datetime(row.get("interval_start")); interval_end = parse_api_datetime(row.get("interval_end")); value = Decimal(str(row["consumption"]))
            except (KeyError, InvalidOperation, OctopusApiError) as exc:
                raise OctopusApiError("Octopus API returned an invalid gas consumption interval") from exc
            if interval_start < start or interval_end > end: continue
            if interval_end - interval_start != HALF_HOUR or value < 0: raise OctopusApiError("Octopus API returned an invalid gas consumption interval")
            slot = (interval_start, interval_end)
            if slot in values and values[slot] != value: raise OctopusApiError("Octopus API returned conflicting gas intervals")
            values[slot] = value
        return [EnergyIntervalInput("octopus_gas", "gas_consumption_kwh", a, b, value * GAS_M3_TO_KWH, "settled") for (a, b), value in sorted(values.items())], set(values)

    def _retry(self, action):
        for attempt in range(3):
            try:
                return action()
            except OctopusApiError as exc:
                if not exc.transient or attempt == 2:
                    raise
                self.sleep(2**attempt)
        raise AssertionError("unreachable")

    def _fail_run(self, run_id: int, start: datetime, end: datetime, dataset: str, exc: OctopusApiError) -> None:
        self.ingestion.finish_run(run_id, status="failed", rows_received=0, rows_written=0, failure_code="octopus_api", failure_detail=str(exc))
        self.ingestion.record_coverage(source_code="octopus_import", dataset=dataset, start=start, end=end, status="failed", run_id=run_id)
        logger.warning("Octopus %s import failed: %s", dataset, type(exc).__name__)

    def _fail_gas_run(self, run_id: int, start: datetime, end: datetime, dataset: str, exc: OctopusApiError) -> None:
        self.ingestion.finish_run(run_id, status="failed", rows_received=0, rows_written=0, failure_code="octopus_api", failure_detail=str(exc))
        self.ingestion.record_coverage(source_code="octopus_gas", dataset=dataset, start=start, end=end, status="failed", run_id=run_id)
        logger.warning("Octopus gas %s import failed: %s", dataset, type(exc).__name__)

    def _record_startup_failure(self, now: datetime, exc: Exception) -> None:
        run_id = self.ingestion.start_run("octopus_import", "consumption_backfill")
        self.ingestion.finish_run(run_id, status="failed", rows_received=0, rows_written=0, failure_code="configuration" if isinstance(exc, OctopusImportConfigurationError) else "octopus_api", failure_detail="Octopus import configuration or account resolution failed")

    def _record_gas_startup_failure(self, now: datetime, exc: Exception) -> None:
        run_id = self.ingestion.start_run("octopus_gas", "gas_consumption_backfill")
        self.ingestion.finish_run(run_id, status="failed", rows_received=0, rows_written=0, failure_code="octopus_api", failure_detail="Octopus gas account resolution failed")
