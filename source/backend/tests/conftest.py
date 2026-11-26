from __future__ import annotations

import os
import re
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.database import get_engine


@pytest.fixture(scope="session")
def postgres_engine():
    """Create a disposable PostgreSQL database for schema integration tests."""
    original_url = os.environ["DATABASE_URL"]
    base_url = make_url(original_url)
    database_name = f"energy_test_{uuid.uuid4().hex}"
    assert re.fullmatch(r"[a-z0-9_]+", database_name)
    admin_engine = create_engine(base_url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin_engine.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')

    test_url = base_url.set(database=database_name)
    os.environ["DATABASE_URL"] = test_url.render_as_string(hide_password=False)
    get_engine.cache_clear()
    config = Config("alembic.ini")
    command.upgrade(config, "0001_collector_heartbeat")
    with get_engine().connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0001_collector_heartbeat"
    command.upgrade(config, "head")
    command.upgrade(config, "head")
    engine = get_engine()
    yield engine

    engine.dispose()
    get_engine.cache_clear()
    os.environ["DATABASE_URL"] = original_url
    with admin_engine.connect() as connection:
        connection.execute(
            text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :name"),
            {"name": database_name},
        )
        connection.exec_driver_sql(f'DROP DATABASE "{database_name}"')
    admin_engine.dispose()


@pytest.fixture(autouse=True)
def clean_schema(postgres_engine):
    tables = (
        "coverage_windows, sync_checkpoints, ingestion_runs, telemetry_rollups, "
        "telemetry_latest, telemetry_samples, tariff_rates, energy_intervals"
    )
    with postgres_engine.begin() as connection:
        connection.exec_driver_sql(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    yield

