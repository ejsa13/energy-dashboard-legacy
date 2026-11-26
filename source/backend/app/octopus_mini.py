"""Octopus Home Mini live telemetry worker."""
from __future__ import annotations
import logging, os, time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from app.database import get_engine
from app.octopus import OctopusApiError, OctopusClient, parse_api_datetime
from app.repositories import LiveProviderHealthRepository, TelemetryRepository, TelemetrySampleInput

logger = logging.getLogger("collector.octopus_mini")

MAP = {"consumption":"octopus_mini_consumption_power", "export":"octopus_mini_export_power", "demand":"octopus_mini_demand_power", "consumptionDelta":"octopus_mini_consumption_delta", "costDelta":"octopus_mini_cost_delta", "costDeltaWithTax":"octopus_mini_cost_delta_inc_vat"}
class OctopusMiniWorker:
    def __init__(self, client_factory=OctopusClient, now=lambda: datetime.now(timezone.utc), sleep=time.sleep, dashboard_id: str | None = None, env: dict[str, str] | None = None):
        self.client_factory,self.now,self.sleep=client_factory,now,sleep; self.dashboard_id=dashboard_id; self.env=env if env is not None else os.environ; self.device_id=self.env.get("OCTOPUS_MINI_DEVICE_ID") or None; self.client=None; self.retry_after=None
    def run_once(self):
        now=self.now(); health=LiveProviderHealthRepository(get_engine(), self.dashboard_id)
        if self.retry_after is not None and now < self.retry_after:
            return 0
        health.update(source_code="octopus_mini",dataset="telemetry",state="syncing",attempted_at=now)
        try:
            key=self.env.get("OCTOPUS_API_KEY",""); account=self.env.get("OCTOPUS_ACCOUNT_NUMBER","")
            if not key or not account: raise OctopusApiError("Octopus account credentials are not configured")
            if self.client is None:
                self.client = self.client_factory(key)
            if self.device_id is None: self.device_id=self.client.discover_mini_device(account_number=account)
            rows=self._retry(lambda: self.client.mini_telemetry(device_id=self.device_id,start=now-timedelta(minutes=2),end=now))
            samples=[]; newest=None
            for row in rows:
                stamp=parse_api_datetime(row.get("readAt")); newest=max(newest,stamp) if newest else stamp
                for field,metric in MAP.items():
                    value=row.get(field)
                    if value is None: continue
                    try: number=Decimal(str(value))
                    except (InvalidOperation, ValueError): raise OctopusApiError("Octopus Mini returned an invalid numeric measurement")
                    samples.append(TelemetrySampleInput("octopus_mini",metric,stamp,now,value_number=number))
            TelemetryRepository(get_engine(), self.dashboard_id).insert_batch(samples)
            values = ", ".join(f"{sample.metric_code}={sample.value_number}" for sample in samples)
            logger.info("Octopus Mini poll completed: %s reading(s)%s", len(samples), f"; {values}" if values else "")
            health.update(source_code="octopus_mini",dataset="telemetry",state="healthy",attempted_at=now,sample_at=newest)
            return len(samples)
        except OctopusApiError as exc:
            reason = str(exc)[:240]
            normalized_reason = reason.lower()
            if any(marker in normalized_reason for marker in ("jwt", "token", "authentication", "unauthor")):
                # Discard the cached GraphQL client so the next collection
                # obtains a completely new Octopus session.
                self.client = None
            if "too many requests" in normalized_reason or "rate limit" in normalized_reason:
                # Octopus GraphQL limits the Home Mini endpoint; wait for its window
                # instead of turning one rejected request into a retry storm.
                self.retry_after = now + timedelta(minutes=5)
            logger.warning("Octopus Mini poll failed: %s", reason)
            health.update(source_code="octopus_mini",dataset="telemetry",state="failed",attempted_at=now,reason=reason); return 0
    def _retry(self, action):
        last=None
        for attempt in range(3):
            try: return action()
            except OctopusApiError as exc:
                last=exc
                if not exc.transient or attempt==2: raise
                self.sleep(2**attempt)
        raise last
