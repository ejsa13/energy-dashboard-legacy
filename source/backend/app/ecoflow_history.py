"""Strict, credential-safe adapter for EcoFlow app-cloud historical series.

The app endpoint is unofficial.  A response must match the narrow documented
adapter contract below before any values are written; unknown payloads are
recorded as failed imports without retaining their contents.

Authentication shape adapted from the MIT-licensed EcoFlow Energy HA project.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib import error, parse, request
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.database import get_engine
from app.octopus import parse_api_datetime
from app.repositories import CatalogRepository, EnergyIntervalInput, EnergyIntervalRepository, IngestionRepository, TelemetryRepository, TelemetrySampleInput

logger = logging.getLogger("collector.ecoflow_history")
LONDON = ZoneInfo("Europe/London")
RECONCILIATION_DAYS = 14
DAILY_BACKFILL_BATCH_SIZE = 30
API_BASE = "https://api-e.ecoflow.com"
HISTORY_PATH = "/iot-open/sign/device/quota/data"
QUOTA_ALL_PATH = "/iot-open/sign/device/quota/all"
HISTORY_CODE = "JT303_Dashboard_Overview_Summary_Day"

class EcoFlowHistoryError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message); self.transient = transient

class EcoFlowHistoryConfigurationError(ValueError): pass

@dataclass(frozen=True, slots=True)
class EcoFlowHistoryConfig:
    access_key: str; secret_key: str; serial: str; start: datetime
    @classmethod
    def from_environment(cls, env: dict[str, str] | None = None) -> "EcoFlowHistoryConfig":
        values = env or os.environ
        required = ("ECOFLOW_ACCESS_KEY", "ECOFLOW_SECRET_KEY", "ECOFLOW_POWEROCEAN_SERIAL", "ECOFLOW_START_DATE")
        if any(not values.get(key, "").strip() for key in required): raise EcoFlowHistoryConfigurationError("Missing required EcoFlow collector configuration")
        try: parsed = parse_api_datetime(values["ECOFLOW_START_DATE"])
        except Exception as exc: raise EcoFlowHistoryConfigurationError("ECOFLOW_START_DATE must be an aware timestamp") from exc
        local = parsed.astimezone(LONDON)
        return cls(values["ECOFLOW_ACCESS_KEY"].strip(), values["ECOFLOW_SECRET_KEY"], values["ECOFLOW_POWEROCEAN_SERIAL"].strip(), datetime.combine(date(local.year, local.month, 1), datetime_time.min, LONDON).astimezone(timezone.utc))

def month_ranges(start: datetime, end: datetime):
    cursor = datetime.combine(date(start.astimezone(LONDON).year, start.astimezone(LONDON).month, 1), datetime_time.min, LONDON)
    while cursor.astimezone(timezone.utc) < end:
        following = datetime(cursor.year + (cursor.month == 12), 1 if cursor.month == 12 else cursor.month + 1, 1, tzinfo=LONDON)
        yield max(start, cursor.astimezone(timezone.utc)), min(end, following.astimezone(timezone.utc)); cursor = following

class EcoFlowAppHistoryClient:
    """Official signed Developer API client, adapted from local energy CLI."""
    def __init__(self, access_key: str, secret_key: str, *, transport: Callable[[request.Request, float], bytes] | None = None):
        self.access_key, self.secret_key, self.transport = access_key, secret_key, transport or self._urlopen
    @staticmethod
    def _urlopen(req: request.Request, timeout: float) -> bytes:
        with request.urlopen(req, timeout=timeout) as response: return response.read()
    def _json(self, req: request.Request) -> dict[str, Any]:
        try: raw = self.transport(req, 20)
        except error.HTTPError as exc: raise EcoFlowHistoryError(f"EcoFlow app API returned HTTP {exc.code}", transient=exc.code == 429 or exc.code >= 500) from exc
        except Exception as exc: raise EcoFlowHistoryError("EcoFlow app API is temporarily unavailable", transient=True) from exc
        try: value = json.loads(raw.decode(), parse_float=Decimal)
        except Exception as exc: raise EcoFlowHistoryError("EcoFlow app API returned malformed JSON") from exc
        if not isinstance(value, dict): raise EcoFlowHistoryError("EcoFlow app API returned an invalid payload")
        return value
    def _signed_headers(self, body: dict[str, Any]) -> dict[str, str]:
        def flatten(value: Any, prefix: str = "") -> list[tuple[str, str]]:
            if isinstance(value, dict): return [pair for key in sorted(value) for pair in flatten(value[key], f"{prefix}.{key}" if prefix else key)]
            if isinstance(value, list): return [pair for index, child in enumerate(value) for pair in flatten(child, f"{prefix}[{index}]")]
            return [(prefix, "" if value is None else str(value))]
        nonce = str(int(time.time() * 1000000))[-6:]
        timestamp = str(int(time.time() * 1000))
        parts = flatten(body) + [("accessKey", self.access_key), ("nonce", nonce), ("timestamp", timestamp)]
        signature = hmac.new(self.secret_key.encode(), "&".join(f"{key}={value}" for key, value in parts).encode(), hashlib.sha256).hexdigest()
        return {"accessKey": self.access_key, "nonce": nonce, "timestamp": timestamp, "sign": signature, "Content-Type": "application/json;charset=UTF-8"}
    def validate_device(self, serial: str) -> None:
        # Quota history itself validates the configured Developer API device access.
        if not serial: raise EcoFlowHistoryError("Configured PowerOcean device is missing")
    def history(self, serial: str, start: datetime, end: datetime) -> dict[str, Any]:
        end_for_query = end - timedelta(seconds=1)
        body = {"sn": serial, "params": {"code": HISTORY_CODE, "beginTime": start.astimezone(LONDON).strftime("%Y-%m-%d %H:%M:%S"), "endTime": end_for_query.astimezone(LONDON).strftime("%Y-%m-%d %H:%M:%S")}}
        response = self._json(request.Request(API_BASE + HISTORY_PATH, data=json.dumps(body, separators=(",", ":")).encode(), headers=self._signed_headers(body)))
        if str(response.get("code")) not in {"0", "None"} and response.get("code") not in {0, None}: raise EcoFlowHistoryError("EcoFlow Developer API rejected the request")
        return response
    def quota_all(self, serial: str) -> dict[str, Any]:
        """Read the current quota map, including the reported system capacity."""
        self.validate_device(serial)
        params = {"sn": serial}
        response = self._json(request.Request(API_BASE + QUOTA_ALL_PATH + "?" + parse.urlencode(params), headers=self._signed_headers(params)))
        if str(response.get("code")) not in {"0", "None"} and response.get("code") not in {0, None}: raise EcoFlowHistoryError("EcoFlow Developer API rejected the quota request")
        return response

CANONICAL = {
    "from_solar": "solar_generation_energy",
    "to_house": "home_consumption_energy",
    "from_grid": "grid_import_energy",
    "to_grid": "grid_export_energy",
    "to_battery": "battery_charge_energy",
    "from_battery": "battery_discharge_energy",
    # Optional EV charger direction. It is stored when the Power Pulse is
    # installed and reported, but is deliberately not required for coverage.
    "to_power_pulse": "ecoflow_to_power_pulse",
    "to_powerpulse": "ecoflow_to_power_pulse",
}

# The EcoFlow app and Developer API use different labels across firmware
# releases.  Only an explicitly named GBP value is eligible to replace the
# calculated dashboard result: a kWh value, a percentage, or an unnamed
# numeric field must never be mistaken for money.
REPORTED_DAILY_EARNINGS_NAMES = {
    "aggregated_savings",
    "total_earnings",
    "total_savings",
}
REPORTED_DAILY_EARNINGS_METRIC = "ecoflow_reported_daily_savings_gbp"


def _reported_daily_earnings(item: dict[str, Any]) -> Decimal | None:
    """Return a provider-reported daily GBP saving, if this item is one."""
    name = item.get("indexName")
    if not isinstance(name, str):
        return None
    code = "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")
    unit = str(item.get("unit") or "").strip().lower().replace(" ", "")
    if code not in REPORTED_DAILY_EARNINGS_NAMES or unit not in {"gbp", "£", "£gbp"}:
        return None
    try:
        value = Decimal(str(item.get("indexValue")))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not value.is_finite() or value < 0:
        raise EcoFlowHistoryError("EcoFlow reported daily savings is invalid")
    return value

class EcoFlowHistoricalImporter:
    def __init__(self, *, client_factory=EcoFlowAppHistoryClient, now=lambda: datetime.now(timezone.utc), sleep=time.sleep, env: dict[str, str] | None = None, dashboard_id: str | None = None):
        self.client_factory, self.now, self.sleep, self.env, self.dashboard_id = client_factory, now, sleep, env, dashboard_id; engine = get_engine(); self.catalog = CatalogRepository(engine); self.energy = EnergyIntervalRepository(engine, dashboard_id); self.telemetry = TelemetryRepository(engine, dashboard_id); self.ingestion = IngestionRepository(engine, dashboard_id)
    def run(self) -> None:
        now = self.now().astimezone(timezone.utc)
        try:
            self.catalog.seed(self.dashboard_id)
            config = EcoFlowHistoryConfig.from_environment(self.env)
            client = self.client_factory(config.access_key, config.secret_key)
            self._retry(lambda: client.validate_device(config.serial))
        except (EcoFlowHistoryError, EcoFlowHistoryConfigurationError) as exc:
            # This is deliberately limited to the adapter's safe messages: no
            # request body, credentials or device identifier is included.
            logger.warning("EcoFlow history setup failed: %s", str(exc)[:240])
            self._startup_failed(str(exc)); return
        local_today = now.astimezone(LONDON).date()
        day_start = datetime.combine(local_today, datetime_time.min, LONDON).astimezone(timezone.utc)
        next_day = datetime.combine(local_today + timedelta(days=1), datetime_time.min, LONDON).astimezone(timezone.utc)
        dataset = "ecoflow_history_daily"
        # The dashboard endpoint returns authoritative totals for a whole local
        # day. Hourly responses cannot be safely added, so each day is imported
        # and validated independently. A day is complete only after all six
        # directional energy fields were persisted.
        dataset = "ecoflow_daily_history"
        local_today = now.astimezone(LONDON).date()
        # Keep the configured month for fiscal-year summaries, but do not
        # request days before the actual installation timestamp.
        first_day = parse_api_datetime((self.env or os.environ)["ECOFLOW_START_DATE"]).astimezone(LONDON).date()
        first_start = datetime.combine(first_day, datetime_time.min, LONDON).astimezone(timezone.utc)
        # Discard coverage attempts from before installation; EcoFlow correctly
        # returns no daily summary there, so they cannot represent missing data.
        with get_engine().begin() as connection:
            connection.execute(text("""DELETE FROM coverage_windows cw
              USING data_sources ds WHERE cw.source_id=ds.id AND ds.code='ecoflow_powerocean'
              AND ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
              AND cw.dataset=:dataset AND cw.window_start < :first_start"""), {"dataset": dataset, "first_start": first_start})
        with get_engine().connect() as connection:
            coverage_by_day = {
                row.window_start.astimezone(LONDON).date(): row.status
                for row in connection.execute(text("""SELECT cw.window_start, cw.status FROM coverage_windows cw
                  JOIN data_sources ds ON ds.id=cw.source_id
                  WHERE ds.code='ecoflow_powerocean' AND cw.dataset=:dataset
                    AND ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
                """), {"dataset": dataset})
            }
        missing_days = [
            first_day + timedelta(days=offset)
            for offset in range((local_today - first_day).days + 1)
            if coverage_by_day.get(first_day + timedelta(days=offset)) != "complete"
        ]
        if missing_days:
            # Work oldest-first so the coverage gap visibly advances. A fixed
            # batch prevents a large first import from monopolising the live
            # collector or exceeding the provider's API budget.
            # Prefer never-attempted windows. Failed windows are retried once
            # the forward scan reaches the end, preventing one unavailable
            # historical day from permanently blocking the remaining range.
            planned_days = sorted(
                missing_days,
                key=lambda candidate: (candidate in coverage_by_day, candidate),
            )[:DAILY_BACKFILL_BATCH_SIZE]
            mode = "backfill"
        else:
            planned_start = max(first_day, local_today - timedelta(days=RECONCILIATION_DAYS))
            planned_days = [planned_start + timedelta(days=offset)
                            for offset in range((local_today - planned_start).days + 1)]
            mode = "sync"
        days = [
            (datetime.combine(candidate, datetime_time.min, LONDON).astimezone(timezone.utc),
             datetime.combine(candidate + timedelta(days=1), datetime_time.min, LONDON).astimezone(timezone.utc))
            for candidate in planned_days
        ]
        logger.info("EcoFlow %s will process %s daily summary window(s).", mode, len(days))
        for start, end in days:
            logger.info("EcoFlow daily summary: %s.", start.astimezone(LONDON).date().isoformat())
            run_id = self.ingestion.start_run("ecoflow_powerocean", f"{dataset}_{mode}", start, end)
            try:
                response = self._retry(lambda start=start, end=end: client.history(config.serial, start, end))
                energies, telemetry, metrics = self._decode(response, start, end)
                expected = {"solar_generation_energy", "ecoflow_to_home", "grid_import_energy", "grid_export_energy", "battery_charge_energy", "battery_discharge_energy"}
                received = {item.metric_code for item in energies}
                if not expected.issubset(received):
                    raise EcoFlowHistoryError("EcoFlow daily summary is incomplete")
                reported = self._reported_daily_earnings(response)
                if reported is not None:
                    metrics.append(self._reported_earnings_metric())
                    self._delete_reported_earnings_sample(start)
                    telemetry.append(self._reported_earnings_sample(start, reported))
                self.catalog.register_metrics(list({str(metric["code"]): metric for metric in metrics}.values()))
                # Replace the current day because EcoFlow reports its cumulative
                # total as it changes. Past daily intervals are immutable.
                if start.astimezone(LONDON).date() == local_today:
                    with get_engine().begin() as connection:
                        connection.execute(text("""DELETE FROM energy_intervals ei
                            USING data_sources ds WHERE ei.source_id = ds.id
                              AND ds.code = 'ecoflow_powerocean'
                              AND ei.interval_start = :day_start"""), {"day_start": start})
                # Daily-history coverage is defined by the six directional
                # energy readings. Telemetry is stored as a side effect and
                # may be de-duplicated, so it must not distort these counters.
                energy_written = self.energy.upsert_batch(energies)
                self.telemetry.insert_batch(telemetry)
                self.ingestion.finish_run(run_id, status="complete", rows_received=len(energies), rows_written=energy_written)
                self.ingestion.record_coverage(source_code="ecoflow_powerocean", dataset=dataset, start=start, end=end, status="complete", run_id=run_id)
                self.ingestion.update_checkpoint(source_code="ecoflow_powerocean", dataset=dataset, watermark=end, cursor=None, succeeded_at=self.now())
                logger.info("EcoFlow daily summary persisted: %s received / %s written.", len(energies), energy_written)
            except EcoFlowHistoryError as exc:
                logger.warning("EcoFlow daily summary failed for %s: %s", start.astimezone(LONDON).date().isoformat(), str(exc)[:240])
                self.ingestion.finish_run(run_id, status="failed", rows_received=0, rows_written=0, failure_code="ecoflow_app", failure_detail=str(exc))
                # A transient/partial response must never erase proof that this
                # day was already fully persisted by an earlier import.
                with get_engine().connect() as connection:
                    already_complete = connection.execute(text("""SELECT EXISTS (
                      SELECT 1 FROM coverage_windows cw JOIN data_sources ds ON ds.id=cw.source_id
                      WHERE ds.code='ecoflow_powerocean' AND cw.dataset=:dataset
                        AND ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
                        AND cw.window_start=:start AND cw.window_end=:end AND cw.status='complete')"""), {"dataset": dataset, "start": start, "end": end}).scalar()
                if not already_complete:
                    self.ingestion.record_coverage(source_code="ecoflow_powerocean", dataset=dataset, start=start, end=end, status="failed", run_id=run_id)
                # One partial provider response must not strand all later
                # missing coverage. Keep this failed window retryable on the
                # next pass and continue with the remainder of the batch.
                continue
        # Once the provider has exposed a reported money field, backfill its
        # earlier daily values. This is deliberately independent from energy
        # coverage: a response may contain the app total even when one of the
        # directional figures is not available.
        if self._has_reported_daily_earnings():
            self._backfill_reported_daily_earnings(client, config, first_day, local_today)
        try:
            self._sync_solar_period_totals(client, config, now)
        except EcoFlowHistoryError as exc:
            # Preserve the last authoritative totals and continue hourly backfill.
            logger.warning("EcoFlow period-total refresh was incomplete: %s", type(exc).__name__)
        # Today's quota endpoint supplies completed hourly readings too.  Keep
        # them refreshed while the daily summary is still changing, rather
        # than making the dashboard wait until tomorrow for an hourly chart.
        self._sync_current_hourly(client, config, now)
        # Process all remaining finished days in this catch-up run. Stop if
        # EcoFlow still returns an incomplete day so the collector can retry it
        # on its next pass without fabricating any readings.
        while True:
            before = self._hourly_pending_count(first_day, local_today)
            if before == 0:
                break
            self._sync_one_hourly_day(client, config, first_day, local_today)
            if self._hourly_pending_count(first_day, local_today) >= before:
                break

    def _sync_current_hourly(self, client: EcoFlowAppHistoryClient, config: EcoFlowHistoryConfig, now: datetime) -> None:
        """Persist each closed current-day hour returned by EcoFlow.

        The API can still be settling the newest hour, so incomplete responses
        are left for the next five-minute refresh.  Earlier successful hours
        remain visible immediately and are reconciled by the completed-day
        importer on the following day.
        """
        local_now = now.astimezone(LONDON)
        day_start = datetime.combine(local_now.date(), datetime_time.min, LONDON).astimezone(timezone.utc)
        closed_hour = local_now.replace(minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        if closed_hour <= day_start:
            return
        expected = {"solar_generation_energy", "ecoflow_to_home", "grid_import_energy", "grid_export_energy", "battery_charge_energy", "battery_discharge_energy"}
        energies: list[EnergyIntervalInput] = []
        telemetry: list[TelemetrySampleInput] = []
        metrics: list[dict[str, Any]] = []
        hour_start = day_start
        while hour_start < closed_hour:
            hour_end = hour_start + timedelta(hours=1)
            try:
                response = self._retry(lambda hour_start=hour_start, hour_end=hour_end: client.history(config.serial, hour_start, hour_end))
                hourly_energy, hourly_telemetry, hourly_metrics = self._decode(response, hour_start, hour_end)
            except EcoFlowHistoryError:
                hour_start = hour_end
                continue
            if expected.issubset({item.metric_code for item in hourly_energy}):
                energies.extend(hourly_energy)
                telemetry.extend(hourly_telemetry)
                metrics.extend(hourly_metrics)
            hour_start = hour_end
        if metrics:
            self.catalog.register_metrics(list({str(metric["code"]): metric for metric in metrics}.values()))
        if energies:
            self.energy.upsert_batch(energies)
            self.telemetry.insert_batch(telemetry)

    def _hourly_pending_count(self, first_day: date, local_today: date) -> int:
        dataset = "ecoflow_hourly_history"
        with get_engine().connect() as connection:
            complete = {row.window_start.astimezone(LONDON).date() for row in connection.execute(text("""SELECT cw.window_start FROM coverage_windows cw
              JOIN data_sources ds ON ds.id=cw.source_id WHERE ds.code='ecoflow_powerocean'
                AND ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
                AND cw.dataset=:dataset AND cw.status='complete'"""), {"dataset": dataset}).mappings()}
        return sum(1 for offset in range(1, (local_today - first_day).days + 1)
                   if local_today - timedelta(days=offset) not in complete)

    def _sync_one_hourly_day(self, client: EcoFlowAppHistoryClient, config: EcoFlowHistoryConfig, first_day: date, local_today: date) -> None:
        """Backfill one completed local day per run without overwhelming EcoFlow's API.

        A day is only marked complete after all 24 hourly responses contain all
        six directional energy readings.  The newest missing day is chosen so
        the dashboard gains useful detail quickly while older history catches up.
        """
        dataset = "ecoflow_hourly_history"
        with get_engine().connect() as connection:
            coverage = list(connection.execute(text("""SELECT cw.window_start, cw.status, cw.recorded_at FROM coverage_windows cw
              JOIN data_sources ds ON ds.id=cw.source_id
              WHERE ds.code='ecoflow_powerocean' AND cw.dataset=:dataset
                AND ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
                AND cw.status IN ('complete', 'failed')"""), {"dataset": dataset}).mappings())
        complete = {row["window_start"].astimezone(LONDON).date() for row in coverage if row["status"] == "complete"}
        retryable = sorted(
            (row["window_start"].astimezone(LONDON).date() for row in coverage
             if row["status"] == "failed"),
            reverse=True,
        )
        # Failed days are retried on every bulk catch-up pass. A successful
        # retry overwrites the failed coverage immediately.
        candidate = retryable[0] if retryable else next(
            (day for offset in range(1, (local_today - first_day).days + 1)
             if (day := local_today - timedelta(days=offset)) not in complete),
            None,
        )
        if candidate is None:
            return
        start = datetime.combine(candidate, datetime_time.min, LONDON).astimezone(timezone.utc)
        end = datetime.combine(candidate + timedelta(days=1), datetime_time.min, LONDON).astimezone(timezone.utc)
        run_id = self.ingestion.start_run("ecoflow_powerocean", f"{dataset}_backfill", start, end)
        expected = {"solar_generation_energy", "ecoflow_to_home", "grid_import_energy", "grid_export_energy", "battery_charge_energy", "battery_discharge_energy"}
        energies: list[EnergyIntervalInput] = []
        telemetry: list[TelemetrySampleInput] = []
        metrics: list[dict[str, Any]] = []
        try:
            hour_start = start
            while hour_start < end:
                hour_end = min(hour_start + timedelta(hours=1), end)
                local_hour = hour_start.astimezone(LONDON).strftime("%H:%M")
                response = self._retry(lambda hour_start=hour_start, hour_end=hour_end: client.history(config.serial, hour_start, hour_end))
                hourly_energy, hourly_telemetry, hourly_metrics = self._decode(response, hour_start, hour_end)
                # EcoFlow can briefly return a partial interval while its
                # history service settles. Retry that hour before considering
                # the day incomplete.
                for attempt in range(3):
                    if expected.issubset({item.metric_code for item in hourly_energy}):
                        break
                    if attempt == 2:
                        raise EcoFlowHistoryError(f"EcoFlow hourly history is incomplete for {candidate.isoformat()} {local_hour}")
                    self.sleep(2**attempt)
                    response = self._retry(lambda hour_start=hour_start, hour_end=hour_end: client.history(config.serial, hour_start, hour_end))
                    hourly_energy, hourly_telemetry, hourly_metrics = self._decode(response, hour_start, hour_end)
                energies.extend(hourly_energy)
                telemetry.extend(hourly_telemetry)
                metrics.extend(hourly_metrics)
                self.sleep(0.15)
                hour_start = hour_end
            self.catalog.register_metrics(list({str(metric["code"]): metric for metric in metrics}.values()))
            written = self.energy.upsert_batch(energies) + self.telemetry.insert_batch(telemetry)
            self.ingestion.finish_run(run_id, status="complete", rows_received=len(energies) + len(telemetry), rows_written=written)
            self.ingestion.record_coverage(source_code="ecoflow_powerocean", dataset=dataset, start=start, end=end, status="complete", run_id=run_id)
            self.ingestion.update_checkpoint(source_code="ecoflow_powerocean", dataset=dataset, watermark=end, cursor=None, succeeded_at=self.now())
        except EcoFlowHistoryError as exc:
            self.ingestion.finish_run(run_id, status="failed", rows_received=len(energies) + len(telemetry), rows_written=0, failure_code="ecoflow_app", failure_detail=str(exc))
            self.ingestion.record_coverage(source_code="ecoflow_powerocean", dataset=dataset, start=start, end=end, status="failed", run_id=run_id)
            logger.warning("EcoFlow hourly history import failed for %s: %s", candidate, type(exc).__name__)

    @staticmethod
    def _reported_earnings_metric() -> dict[str, str]:
        return {
            "code": REPORTED_DAILY_EARNINGS_METRIC,
            "display_name": "Reported daily aggregated savings",
            "measurement_kind": "diagnostic",
            "canonical_unit": "GBP",
            "value_type": "number",
        }

    def _reported_earnings_sample(self, start: datetime, value: Decimal) -> TelemetrySampleInput:
        return TelemetrySampleInput(
            "ecoflow_powerocean", REPORTED_DAILY_EARNINGS_METRIC,
            start, self.now(), value_number=value,
        )

    @staticmethod
    def _delete_reported_earnings_sample(start: datetime) -> None:
        """Allow the in-progress local day's cumulative app report to refresh."""
        with get_engine().begin() as connection:
            connection.execute(text("""DELETE FROM telemetry_samples ts USING data_sources ds
              WHERE ts.source_id=ds.id AND ds.code='ecoflow_powerocean'
                AND ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
                AND ts.metric_code=:metric AND ts.sampled_at=:sampled_at"""), {
                    "metric": REPORTED_DAILY_EARNINGS_METRIC, "sampled_at": start,
            })

    def _has_reported_daily_earnings(self) -> bool:
        with get_engine().connect() as connection:
            return bool(connection.execute(text("""SELECT EXISTS (
              SELECT 1 FROM telemetry_samples ts JOIN data_sources ds ON ds.id=ts.source_id
              WHERE ds.code='ecoflow_powerocean'
                AND ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
                AND ts.metric_code=:metric)"""), {
                "metric": REPORTED_DAILY_EARNINGS_METRIC,
            }).scalar())

    def _backfill_reported_daily_earnings(self, client: EcoFlowAppHistoryClient, config: EcoFlowHistoryConfig, first_day: date, local_today: date) -> None:
        """Fetch daily app totals that predate the first reported-savings sync."""
        dataset = "ecoflow_reported_daily_savings"
        with get_engine().connect() as connection:
            complete = {
                row.window_start.astimezone(LONDON).date()
                for row in connection.execute(text("""SELECT cw.window_start FROM coverage_windows cw
                  JOIN data_sources ds ON ds.id=cw.source_id WHERE ds.code='ecoflow_powerocean'
                    AND ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
                    AND cw.dataset=:dataset AND cw.status='complete'"""), {"dataset": dataset})
            }
        self.catalog.register_metrics([self._reported_earnings_metric()])
        cursor = first_day
        while cursor <= local_today:
            if cursor in complete:
                cursor += timedelta(days=1)
                continue
            start = datetime.combine(cursor, datetime_time.min, LONDON).astimezone(timezone.utc)
            end = datetime.combine(cursor + timedelta(days=1), datetime_time.min, LONDON).astimezone(timezone.utc)
            run_id = self.ingestion.start_run("ecoflow_powerocean", dataset, start, end)
            try:
                response = self._retry(lambda start=start, end=end: client.history(config.serial, start, end))
                reported = self._reported_daily_earnings(response)
                if reported is not None:
                    self._delete_reported_earnings_sample(start)
                    written = self.telemetry.insert_batch([self._reported_earnings_sample(start, reported)])
                else:
                    written = 0
                self.ingestion.finish_run(run_id, status="complete", rows_received=1 if reported is not None else 0, rows_written=written)
                self.ingestion.record_coverage(source_code="ecoflow_powerocean", dataset=dataset, start=start, end=end, status="complete", run_id=run_id)
            except EcoFlowHistoryError as exc:
                self.ingestion.finish_run(run_id, status="failed", rows_received=0, rows_written=0, failure_code="ecoflow_app", failure_detail=str(exc))
                self.ingestion.record_coverage(source_code="ecoflow_powerocean", dataset=dataset, start=start, end=end, status="failed", run_id=run_id)
                logger.warning("EcoFlow reported savings backfill failed for %s: %s", cursor.isoformat(), type(exc).__name__)
                return
            cursor += timedelta(days=1)

    @staticmethod
    def _solar_total(response: dict[str, Any]) -> Decimal:
        data = response.get("data", {})
        if isinstance(data, dict): data = data.get("data", {})
        if isinstance(data, dict): data = data.get("data", [])
        if not isinstance(data, list):
            raise EcoFlowHistoryError("EcoFlow summary response does not match the verified adapter contract")
        for item in data:
            if isinstance(item, dict) and item.get("indexName") == "From Solar":
                try:
                    value = Decimal(str(item.get("indexValue")))
                except (InvalidOperation, ValueError) as exc:
                    raise EcoFlowHistoryError("EcoFlow summary contains an invalid solar total") from exc
                if value.is_finite() and value >= 0:
                    return value
        raise EcoFlowHistoryError("EcoFlow summary did not include From Solar")

    def _sync_solar_period_totals(self, client: EcoFlowAppHistoryClient, config: EcoFlowHistoryConfig, now: datetime) -> None:
        """Persist EcoFlow's authoritative range summaries; hourly sums can undercount."""
        local_day = now.astimezone(LONDON).date()
        month_start = datetime.combine(local_day.replace(day=1), datetime_time.min, LONDON).astimezone(timezone.utc)
        next_month = datetime(month_start.astimezone(LONDON).year + (month_start.astimezone(LONDON).month == 12), 1 if month_start.astimezone(LONDON).month == 12 else month_start.astimezone(LONDON).month + 1, 1, tzinfo=LONDON).astimezone(timezone.utc)
        start_local = config.start.astimezone(LONDON)
        fiscal_start = datetime(local_day.year if local_day.month >= start_local.month else local_day.year - 1, start_local.month, 1, tzinfo=LONDON).astimezone(timezone.utc)
        fiscal_end = datetime(fiscal_start.astimezone(LONDON).year + 1, start_local.month, 1, tzinfo=LONDON).astimezone(timezone.utc)
        day_start = datetime.combine(local_day, datetime_time.min, LONDON).astimezone(timezone.utc)
        next_day = datetime.combine(local_day + timedelta(days=1), datetime_time.min, LONDON).astimezone(timezone.utc)
        ranges = {
            "ecoflow_solar_day_kwh": (day_start, next_day),
            "ecoflow_solar_month_kwh": (month_start, next_month),
            "ecoflow_solar_year_kwh": (fiscal_start, fiscal_end),
            "ecoflow_solar_lifetime_kwh": (config.start, next_month),
        }
        self.catalog.register_metrics([{"code": code, "display_name": code.replace("ecoflow_solar_", "Solar ").replace("_kwh", ""), "measurement_kind": "energy", "canonical_unit": "kWh", "value_type": "number"} for code in ranges])
        sampled_at = self.now()
        values = [TelemetrySampleInput("ecoflow_powerocean", code, sampled_at, sampled_at, value_number=self._solar_total(self._retry(lambda start=start, end=end: client.history(config.serial, start, end)))) for code, (start, end) in ranges.items()]
        self.telemetry.insert_batch(values)

    def _decode(self, response: dict[str, Any], start: datetime, end: datetime):
        data = response.get("data", {})
        if isinstance(data, dict): data = data.get("data", {})
        if isinstance(data, dict): data = data.get("data", [])
        if not isinstance(data, list): raise EcoFlowHistoryError("EcoFlow Developer API response does not match the verified adapter contract")
        energies=[]; telemetry=[]; metrics=[]
        for item in data:
            if not isinstance(item, dict) or not isinstance(item.get("indexName"), str): raise EcoFlowHistoryError("EcoFlow Developer API response does not match the verified adapter contract")
            code="".join(c.lower() if c.isalnum() else "_" for c in item["indexName"]).strip("_"); kind="energy"; unit=str(item.get("unit") or "kWh"); mapped=CANONICAL.get(code); metric=mapped or "ecoflow_" + code
            if not mapped: metrics.append({"code":metric,"display_name":item.get("label", code)[:128],"measurement_kind":kind if kind in {"energy","power","ratio","status","diagnostic"} else "diagnostic","canonical_unit":unit[:32],"value_type":"number"})
            raw_value = item.get("indexValue")
            if raw_value is None:
                continue
            try: value=Decimal(str(raw_value))
            except (InvalidOperation, ValueError):
                # App/API history can include a field with no usable value for
                # a particular hour. Absence is more honest than a synthetic 0.
                continue
            if not value.is_finite():
                continue
            if value < 0: raise EcoFlowHistoryError("EcoFlow Developer API response contains negative directional energy")
            if unit.lower() == "kwh": energies.append(EnergyIntervalInput("ecoflow_powerocean", metric, start, end, value, "reported"))
            else: telemetry.append(TelemetrySampleInput("ecoflow_powerocean", metric, start, start, value_number=value))
        return energies, telemetry, metrics

    @staticmethod
    def _reported_daily_earnings(response: dict[str, Any]) -> Decimal | None:
        """Extract the one authoritative money field from a quota response."""
        data = response.get("data", {})
        if isinstance(data, dict):
            data = data.get("data", {})
        if isinstance(data, dict):
            data = data.get("data", [])
        if not isinstance(data, list):
            return None
        reported = [_reported_daily_earnings(item) for item in data if isinstance(item, dict)]
        values = [value for value in reported if value is not None]
        if len(values) > 1 and len(set(values)) != 1:
            raise EcoFlowHistoryError("EcoFlow daily summary has conflicting reported savings")
        return values[0] if values else None
    def _retry(self, action):
        for attempt in range(3):
            try: return action()
            except EcoFlowHistoryError as exc:
                if not exc.transient or attempt == 2: raise
                self.sleep(2**attempt)
    def _startup_failed(self, reason: str) -> None:
        detail = reason[:240] or "EcoFlow app authentication, device discovery, or history contract failed"
        run_id=self.ingestion.start_run("ecoflow_powerocean", "ecoflow_history_backfill"); self.ingestion.finish_run(run_id,status="failed",rows_received=0,rows_written=0,failure_code="ecoflow_app",failure_detail=detail)
