"""Collector process heartbeat and database telemetry maintenance."""
import logging
import json
import hashlib
import queue
import signal
import threading
import time
from urllib import request

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_engine
from app.octopus_import import OctopusHistoricalImporter
from app.ecoflow_history import EcoFlowHistoricalImporter
from app.octopus_mini import OctopusMiniWorker
from app.powerocean_live import PowerOceanLiveWorker
from app.telemetry_maintenance import TelemetryMaintenance
from app import auth, tenant

logger = logging.getLogger("collector")


class CollectorLogRelay(logging.Handler):
    """Mirror collector messages to the dashboard without persisting them."""

    def __init__(self) -> None:
        super().__init__()
        self.lines: queue.Queue[tuple[str | None, str]] = queue.Queue(maxsize=500)
        self.stopped = threading.Event()
        self.worker = threading.Thread(target=self._deliver, name="collector-log-relay", daemon=True)
        self.worker.start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = f"{self.formatter.formatTime(record)} {record.levelname} {record.getMessage()}"
            self.lines.put_nowait((tenant.current_dashboard(), line[:2000]))
        except queue.Full:
            pass

    def _deliver(self) -> None:
        while not self.stopped.is_set():
            try:
                dashboard_id, line = self.lines.get(timeout=0.5)
            except queue.Empty:
                continue
            body = json.dumps({"dashboard_id": dashboard_id, "line": line}).encode()
            try:
                client = request.build_opener()
                client.open(request.Request("http://api:8000/api/internal/collector-log", data=body, headers={"Content-Type": "application/json"}, method="POST"), timeout=1).read()
            except Exception:
                # The API may be restarting; stdout logging remains unaffected.
                pass

    def close(self) -> None:
        self.stopped.set()
        self.worker.join(timeout=1)
        super().close()


def write_heartbeat() -> None:
    with get_engine().begin() as connection:
        connection.execute(text("""
            INSERT INTO collector_heartbeat (name, last_heartbeat)
            VALUES ('primary', CURRENT_TIMESTAMP)
            ON CONFLICT (name) DO UPDATE
            SET last_heartbeat = EXCLUDED.last_heartbeat
        """))


def run_for_dashboards(label: str, worker) -> None:
    """Run a collector job in each configured dashboard's RLS scope."""
    with get_engine().connect() as connection:
        dashboard_ids = [str(row.dashboard_id) for row in connection.execute(text("SELECT dashboard_id FROM dashboard_settings WHERE setup_complete=true")).mappings()]
    for dashboard_id in dashboard_ids:
        scope = tenant.set_dashboard(dashboard_id)
        try:
            config = auth.collector_configuration(dashboard_id)
            if config is not None:
                worker(config, dashboard_id)
        except Exception:
            logger.exception("%s failed for a dashboard.", label)
        finally:
            tenant.reset_dashboard(scope)


def configured_dashboard_configurations() -> dict[str, dict[str, str]]:
    """Return the current, decrypted configuration for every active dashboard.

    Live workers receive this mapping directly instead of reading process-wide
    environment variables.  That keeps credentials and telemetry writes tied to
    the dashboard that owns them.
    """
    with get_engine().connect() as connection:
        dashboard_ids = [str(row.dashboard_id) for row in connection.execute(text("SELECT dashboard_id FROM dashboard_settings WHERE setup_complete=true ORDER BY dashboard_id")).mappings()]
    configurations: dict[str, dict[str, str]] = {}
    for dashboard_id in dashboard_ids:
        try:
            config = auth.collector_configuration(dashboard_id)
            if config is not None:
                configurations[dashboard_id] = config
        except Exception:
            # A single corrupt or temporarily unreadable configuration must not
            # prevent the other dashboards from collecting live telemetry.
            logger.exception("Could not load collector configuration for a dashboard.")
    return configurations


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    relay = CollectorLogRelay()
    relay.setFormatter(logging.Formatter("%(asctime)s"))
    logging.getLogger().addHandler(relay)
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    logger.info("Collector started; starting live telemetry workers and scheduling history imports.")
    failed = False
    maintenance = TelemetryMaintenance(get_engine())
    next_maintenance = 0.0
    next_octopus_import = 0.0
    next_ecoflow_import = 0.0
    import_thread: threading.Thread | None = None
    ecoflow_thread: threading.Thread | None = None
    # dashboard_id -> (thread, stop event, configuration fingerprint).  A
    # dedicated event lets removal or a configuration update restart one worker
    # without interrupting the other configured dashboards.
    mini_workers: dict[str, tuple[threading.Thread, threading.Event, str]] = {}
    live_workers: dict[str, tuple[threading.Thread, threading.Event, str]] = {}
    live_configurations: dict[str, dict[str, str]] = {}
    next_live_configuration_refresh = 0.0

    def start_octopus_import() -> None:
        nonlocal import_thread
        if import_thread is not None and import_thread.is_alive():
            return

        def run_import() -> None:
            logger.info("Octopus history import started.")
            run_for_dashboards("Octopus history import", lambda config, dashboard_id: OctopusHistoricalImporter(env=config, dashboard_id=dashboard_id).run())
            logger.info("Octopus history import finished.")

        import_thread = threading.Thread(target=run_import, name="octopus-import", daemon=True)
        import_thread.start()

    def worker_fingerprint(config: dict[str, str]) -> str:
        # Keep only a digest for change detection; do not retain another copy
        # of a dashboard's credentials in the supervisor state.
        encoded = json.dumps(config, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def run_mini(dashboard_id: str, config: dict[str, str], worker_stop: threading.Event) -> None:
        scope = tenant.set_dashboard(dashboard_id)
        try:
            worker = OctopusMiniWorker(dashboard_id=dashboard_id, env=config)
            logger.info("Octopus Mini worker started; polling every 120 seconds.")
            while not worker_stop.is_set():
                try:
                    worker.run_once()
                except Exception:
                    # run_once handles provider failures itself.  This guard
                    # also recovers from an unexpected parser/client failure.
                    logger.exception("Octopus Mini worker failed unexpectedly; retrying.")
                worker_stop.wait(120)
        finally:
            tenant.reset_dashboard(scope)

    def run_powerocean_live(dashboard_id: str, config: dict[str, str], worker_stop: threading.Event) -> None:
        scope = tenant.set_dashboard(dashboard_id)
        try:
            logger.info("EcoFlow live MQTT worker started.")
            PowerOceanLiveWorker(worker_stop, dashboard_id=dashboard_id, env=config).run()
        except Exception:
            # The supervisor recreates this worker on the next heartbeat.
            logger.exception("PowerOcean live worker stopped unexpectedly.")
        finally:
            tenant.reset_dashboard(scope)

    def start_ecoflow_import() -> None:
        nonlocal ecoflow_thread
        if ecoflow_thread is not None and ecoflow_thread.is_alive():
            return
        def run_import() -> None:
            logger.info("EcoFlow history import started.")
            run_for_dashboards("EcoFlow history import", lambda config, dashboard_id: EcoFlowHistoricalImporter(env=config, dashboard_id=dashboard_id).run())
            logger.info("EcoFlow history import finished.")
        ecoflow_thread = threading.Thread(target=run_import, name="ecoflow-history", daemon=True)
        ecoflow_thread.start()

    def sync_live_workers(configurations: dict[str, dict[str, str]]) -> None:
        """Reconcile dashboard-specific live workers with current settings."""

        def reconcile(
            workers: dict[str, tuple[threading.Thread, threading.Event, str]],
            name: str,
            target,
            is_configured,
        ) -> None:
            for dashboard_id in list(workers):
                if dashboard_id not in configurations or not is_configured(configurations[dashboard_id]):
                    workers[dashboard_id][1].set()
                    workers.pop(dashboard_id, None)
            for dashboard_id, config in configurations.items():
                if not is_configured(config):
                    continue
                fingerprint = worker_fingerprint(config)
                current = workers.get(dashboard_id)
                if current is not None and current[0].is_alive() and current[2] == fingerprint:
                    continue
                if current is not None and current[0].is_alive():
                    # Credentials or device settings changed.  Stop the old
                    # connection before replacing it with the scoped config.
                    current[1].set()
                worker_stop = threading.Event()
                thread = threading.Thread(
                    target=target,
                    args=(dashboard_id, config, worker_stop),
                    name=f"{name}-{dashboard_id[:8]}",
                    daemon=True,
                )
                workers[dashboard_id] = (thread, worker_stop, fingerprint)
                thread.start()

        reconcile(
            mini_workers,
            "octopus-mini",
            run_mini,
            lambda config: bool(config.get("OCTOPUS_API_KEY") and config.get("OCTOPUS_ACCOUNT_NUMBER")),
        )
        reconcile(
            live_workers,
            "powerocean-live",
            run_powerocean_live,
            lambda config: bool(config.get("ECOFLOW_APP_EMAIL") and config.get("ECOFLOW_APP_PASSWORD") and config.get("ECOFLOW_POWEROCEAN_SERIAL")),
        )
    try:
        while not stopped.is_set():
            delay = 10
            try:
                write_heartbeat()
                if time.monotonic() >= next_maintenance:
                    if maintenance.run():
                        logger.info("Telemetry partition and rollup maintenance completed.")
                    next_maintenance = time.monotonic() + 3600
                if time.monotonic() >= next_ecoflow_import:
                    # Keep the two historical provider imports separate. They
                    # share a constrained external API budget and concurrent
                    # catch-up requests can cause EcoFlow rejections.
                    if import_thread is None or not import_thread.is_alive():
                        start_ecoflow_import()
                        next_ecoflow_import = time.monotonic() + 5 * 60
                # Refresh worker configuration periodically and reconcile each
                # dashboard independently.  Dead workers are replaced on the
                # next refresh; a provider failure never blocks another tenant.
                if time.monotonic() >= next_live_configuration_refresh:
                    live_configurations = configured_dashboard_configurations()
                    next_live_configuration_refresh = time.monotonic() + 60
                # Check worker liveness on every heartbeat using the cached
                # configuration.  This gives an unexpected exit a quick
                # restart without repeatedly decrypting every dashboard's
                # credentials.
                sync_live_workers(live_configurations)
                if time.monotonic() >= next_octopus_import:
                    if ecoflow_thread is None or not ecoflow_thread.is_alive():
                        start_octopus_import()
                        next_octopus_import = time.monotonic() + 5 * 60
                if failed:
                    logger.info("Database connection recovered.")
                failed = False
            except SQLAlchemyError:
                # Avoid logging exceptions containing connection URLs or credentials.
                logger.warning("Database unavailable; retrying heartbeat in 5 seconds.")
                failed = True
                delay = 5
            stopped.wait(delay)
    finally:
        if import_thread is not None:
            import_thread.join(timeout=5)
        if ecoflow_thread is not None:
            ecoflow_thread.join(timeout=5)
        for thread, worker_stop, _fingerprint in mini_workers.values():
            worker_stop.set()
        for thread, worker_stop, _fingerprint in live_workers.values():
            worker_stop.set()
        for thread, _worker_stop, _fingerprint in mini_workers.values():
            thread.join(timeout=5)
        for thread, _worker_stop, _fingerprint in live_workers.values():
            thread.join(timeout=5)
        get_engine().dispose()
        logger.info("Collector stopped gracefully.")
        logging.getLogger().removeHandler(relay)
        relay.close()


if __name__ == "__main__":
    main()
