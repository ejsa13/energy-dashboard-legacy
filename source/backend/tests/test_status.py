from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.main import app
from app.status import collector_status, database_ready, octopus_mini_status, read_status
from app.database import EXPECTED_REVISION

NOW = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("age,state", [(0, "healthy"), (30, "healthy"), (30.1, "stale"), (60, "stale")])
def test_freshness_boundary(age, state):
    result = collector_status(NOW - timedelta(seconds=age), NOW)
    assert result.state == state
    assert result.age_seconds == age


def test_no_heartbeat_is_waiting_not_zero_age():
    result = collector_status(None, NOW)
    assert result.state == "waiting"
    assert result.last_heartbeat is None
    assert result.age_seconds is None


@pytest.mark.parametrize("revisions,ready", [([EXPECTED_REVISION], True), (["old"], False), ([], False)])
def test_readiness_requires_expected_migration(revisions, ready):
    engine = MagicMock()
    engine.connect.return_value.__enter__.return_value.execute.return_value.scalars.return_value.all.return_value = revisions
    with patch("app.status.get_engine", return_value=engine):
        assert database_ready() is ready


def test_database_failure_does_not_expose_details():
    engine = MagicMock()
    engine.connect.side_effect = OperationalError("private-url", {}, Exception("password"))
    with patch("app.status.get_engine", return_value=engine):
        assert database_ready() is False
        result = read_status()
    assert result.database == "unavailable"
    assert result.collector.state == "unknown"
    assert all(provider.state == "not_connected" for provider in result.providers.values())
    assert "password" not in result.model_dump_json()


def test_api_liveness_and_readiness_are_independent():
    client = TestClient(app)
    with patch("app.main.database_ready", return_value=False):
        assert client.get("/api/health/live").status_code == 200
        assert client.get("/api/health/ready").status_code == 503
    with patch("app.main.database_ready", return_value=True):
        assert client.get("/api/health/ready").status_code == 200


def test_status_http_contract():
    with patch("app.status.database_ready", return_value=False):
        response = TestClient(app).get("/api/status")
    assert response.status_code == 200
    assert set(response.json()["providers"]) == {"ecoflow", "octopus", "octopus_gas", "octopus_mini"}
    assert response.json()["collector"]["last_heartbeat"] is None


def test_mini_status_reads_the_current_dashboard_health_record():
    observed_dashboards: list[str | None] = []
    now = datetime.now(timezone.utc)

    class FakeHealthRepository:
        def __init__(self, _engine, dashboard_id=None):
            observed_dashboards.append(dashboard_id)

        def get(self, source_code, dataset):
            assert (source_code, dataset) == ("octopus_mini", "telemetry")
            return SimpleNamespace(
                state="healthy",
                last_success_at=now,
                last_sample_at=now - timedelta(minutes=2),
                reason=None,
            )

    with patch("app.repositories.LiveProviderHealthRepository", FakeHealthRepository), patch(
        "app.status.tenant.current_dashboard", return_value="current-dashboard"
    ):
        result = octopus_mini_status()

    assert result.state == "healthy"
    assert observed_dashboards == ["current-dashboard"]
