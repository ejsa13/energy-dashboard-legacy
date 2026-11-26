from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import EXPECTED_REVISION, get_engine
from app.repositories import IngestionRepository
from app import tenant

STALE_AFTER_SECONDS = 30
# The Mini worker polls every two minutes.  Its reading timestamp can be a
# little older than the successful API response, so leave enough room for one
# missed poll before declaring the provider unavailable.
MINI_STALE_AFTER_SECONDS = 300


class CollectorStatus(BaseModel):
    state: Literal["healthy", "stale", "waiting", "unknown"]
    last_heartbeat: datetime | None = None
    age_seconds: float | None = None


class ProviderStatus(BaseModel):
    state: Literal["not_connected", "backfilling", "syncing", "partial", "healthy", "failed"] = "not_connected"
    reason: str = "Provider integration is not connected."
    mqtt_connection: Literal["connecting", "connected", "stale", "disconnected", "unknown"] | None = None
    receiving_data: bool | None = None
    last_sample_at: datetime | None = None


class SystemStatus(BaseModel):
    observed_at: datetime
    database: Literal["ready", "unavailable"]
    collector: CollectorStatus
    providers: dict[str, ProviderStatus]


def collector_status(last: datetime | None, now: datetime) -> CollectorStatus:
    if last is None:
        return CollectorStatus(state="waiting")
    age = max(0.0, (now - last).total_seconds())
    return CollectorStatus(
        state="healthy" if age <= STALE_AFTER_SECONDS else "stale",
        last_heartbeat=last,
        age_seconds=round(age, 1),
    )


def database_ready() -> bool:
    try:
        with get_engine().connect() as connection:
            revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
            return revisions == [EXPECTED_REVISION]
    except SQLAlchemyError:
        return False


def coverage_issue(source_code: str) -> ProviderStatus | None:
    """Return only coverage windows that have not been superseded by a retry."""
    sql = text("""SELECT count(*) filter (where cw.status='partial') AS partial_count,
      count(*) filter (where cw.status='failed') AS failed_count
      FROM coverage_windows cw JOIN data_sources ds ON ds.id=cw.source_id
      WHERE ds.code=:source
        AND ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
        AND NOT EXISTS (
          SELECT 1 FROM coverage_windows newer
          WHERE newer.source_id=cw.source_id AND newer.dataset=cw.dataset
            AND newer.id<>cw.id
            AND newer.window_start<=cw.window_start AND newer.window_end>=cw.window_end
            AND newer.recorded_at>=cw.recorded_at
            AND newer.status IN ('complete', 'partial')
        )""")
    with get_engine().connect() as connection:
        row = connection.execute(sql, {"source": source_code}).mappings().one()
    if row["failed_count"]:
        return ProviderStatus(state="failed", reason=f"{row['failed_count']} import coverage window(s) failed.")
    if row["partial_count"]:
        return ProviderStatus(state="partial", reason=f"{row['partial_count']} import coverage window(s) are incomplete.")
    return None

def octopus_status() -> ProviderStatus:
    try:
        latest = IngestionRepository(get_engine(), tenant.current_dashboard()).latest_run(source_code="octopus_import")
    except SQLAlchemyError:
        return ProviderStatus(state="failed", reason="Octopus import status is unavailable.")
    if latest is None:
        return ProviderStatus()
    if latest.status == "running":
        state = "backfilling" if latest.dataset.endswith("_backfill") else "syncing"
        return ProviderStatus(state=state, reason="Octopus historical import is in progress.")
    if latest.status == "failed":
        return ProviderStatus(state="failed", reason="Octopus historical import needs attention.")
    issue = coverage_issue("octopus_import")
    if issue is None:
        return ProviderStatus(state="healthy", reason="Octopus import history is fully covered.")
    if issue.state == "failed":
        return issue
    # Settlement data arrives after the provider has accepted the request.
    # That affects data completeness, not whether the authenticated provider
    # integration is connected.
    return ProviderStatus(state="healthy", reason=f"Octopus is connected; {issue.reason}")


def octopus_gas_status() -> ProviderStatus:
    try:
        latest = IngestionRepository(get_engine(), tenant.current_dashboard()).latest_run(source_code="octopus_gas")
    except SQLAlchemyError:
        return ProviderStatus(state="failed", reason="Octopus gas status is unavailable.")
    if latest is None:
        return ProviderStatus()
    if latest.status == "running":
        return ProviderStatus(state="backfilling" if latest.dataset.endswith("_backfill") else "syncing", reason="Octopus gas import is in progress.")
    if latest.status == "failed":
        return ProviderStatus(state="failed", reason="Octopus gas import needs attention.")
    issue = coverage_issue("octopus_gas")
    if issue is None:
        return ProviderStatus(state="healthy", reason="Octopus gas history is fully covered.")
    if issue.state == "failed":
        return issue
    return ProviderStatus(state="healthy", reason=f"Octopus gas is connected; {issue.reason}")

def ecoflow_status() -> ProviderStatus:
    try: latest = IngestionRepository(get_engine(), tenant.current_dashboard()).latest_run(source_code="ecoflow_powerocean")
    except SQLAlchemyError: return ProviderStatus(state="failed", reason="EcoFlow history status is unavailable.")
    try:
        from app.repositories import LiveProviderHealthRepository
        live = LiveProviderHealthRepository(get_engine(), tenant.current_dashboard()).get("ecoflow_powerocean", "telemetry")
        if live is not None:
            mqtt_state = live.connection_state or "unknown"
            latest_sample = live.last_sample_at or live.last_success_at
            receiving = bool(latest_sample and (datetime.now(timezone.utc)-latest_sample).total_seconds() <= 35)
            if live.state == "failed": return ProviderStatus(state="failed", reason=live.reason or "EcoFlow live telemetry needs attention.", mqtt_connection=mqtt_state, receiving_data=receiving, last_sample_at=latest_sample)
            if live.state == "syncing": return ProviderStatus(state="syncing", reason=live.reason or "EcoFlow live telemetry is connecting.", mqtt_connection=mqtt_state, receiving_data=receiving, last_sample_at=latest_sample)
            age=(datetime.now(timezone.utc)-(live.last_sample_at or live.last_success_at)).total_seconds() if (live.last_sample_at or live.last_success_at) else 999
            if age <= 60:
                issue = coverage_issue("ecoflow_powerocean")
                if issue is not None:
                    # The inverter is connected, but import completion is a
                    # separate promise. Keep both facts visible to the user.
                    return ProviderStatus(state="partial", reason=f"{issue.reason} Live telemetry received {int(age)} seconds ago.", mqtt_connection=mqtt_state, receiving_data=receiving, last_sample_at=latest_sample)
                return ProviderStatus(state="healthy", reason=f"EcoFlow live telemetry received {int(age)} seconds ago.", mqtt_connection=mqtt_state, receiving_data=receiving, last_sample_at=latest_sample)
    except SQLAlchemyError: pass
    if latest is None: return ProviderStatus()
    if latest.status == "running": return ProviderStatus(state="backfilling" if latest.dataset.endswith("_backfill") else "syncing", reason="EcoFlow history import is in progress.")
    if latest.status == "failed": return ProviderStatus(state="failed", reason="EcoFlow history needs attention.")
    issue = coverage_issue("ecoflow_powerocean")
    return issue or ProviderStatus(state="healthy", reason="EcoFlow history is fully covered.")


def read_status() -> SystemStatus:
    now = datetime.now(timezone.utc)
    collector = CollectorStatus(state="unknown")
    ready = database_ready()
    if ready:
        try:
            with get_engine().connect() as connection:
                last = connection.execute(text(
                    "SELECT last_heartbeat FROM collector_heartbeat WHERE name = 'primary'"
                )).scalar_one_or_none()
            collector = collector_status(last, now)
        except SQLAlchemyError:
            ready = False
    return SystemStatus(
        observed_at=now,
        database="ready" if ready else "unavailable",
        collector=collector,
        providers={
            "ecoflow": ecoflow_status() if ready else ProviderStatus(),
            "octopus": octopus_status() if ready else ProviderStatus(),
            "octopus_gas": octopus_gas_status() if ready else ProviderStatus(),
            "octopus_mini": octopus_mini_status() if ready else ProviderStatus(),
        },
    )

def octopus_mini_status() -> ProviderStatus:
    try:
        from app.repositories import LiveProviderHealthRepository
        row = LiveProviderHealthRepository(get_engine(), tenant.current_dashboard()).get("octopus_mini", "telemetry")
    except SQLAlchemyError:
        return ProviderStatus(state="failed", reason="Octopus Mini status is unavailable.")
    if row is None: return ProviderStatus()
    if row.state == "failed": return ProviderStatus(state="failed", reason=row.reason or "Octopus Mini needs attention.")
    if row.state == "syncing": return ProviderStatus(state="syncing", reason=row.reason or "Octopus Mini is connecting.")
    # A successful authenticated poll proves that Mini is connected.  Its
    # `readAt` value describes when the meter sampled, not when the provider
    # last responded, so use the successful poll time for connection health.
    observed_at = row.last_success_at or row.last_sample_at
    age = (datetime.now(timezone.utc) - observed_at).total_seconds() if observed_at else 999
    if age > MINI_STALE_AFTER_SECONDS:
        return ProviderStatus(state="failed", reason=row.reason or "Octopus Mini has not responded recently.")
    return ProviderStatus(state="healthy", reason=f"Octopus Mini responded {int(max(0, age))} seconds ago.")
