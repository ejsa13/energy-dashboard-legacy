from __future__ import annotations

import csv
import uuid
from io import StringIO

from fastapi.testclient import TestClient
from sqlalchemy import text

from app import auth
from app.main import app


def test_energy_csv_exports_hourly_ecoflow_and_settled_octopus_data(postgres_engine):
    token = uuid.uuid4().hex
    with postgres_engine.begin() as conn:
        email = f"{uuid.uuid4().hex}@example.com"
        user_id = conn.execute(
            text("INSERT INTO users(email, google_subject) VALUES (:email, :email) RETURNING id"),
            {"email": email},
        ).scalar_one()
        dashboard_id = conn.execute(
            text("INSERT INTO dashboard_accounts(owner_user_id) VALUES (:owner) RETURNING id"),
            {"owner": user_id},
        ).scalar_one()
        conn.execute(
            text("INSERT INTO dashboard_memberships(dashboard_id, user_id) VALUES (:dashboard, :user)"),
            {"dashboard": dashboard_id, "user": user_id},
        )
        conn.execute(
            text("INSERT INTO dashboard_settings(dashboard_id, setup_complete) VALUES (:dashboard, true)"),
            {"dashboard": dashboard_id},
        )
        conn.execute(
            text("INSERT INTO user_sessions(token_hash, user_id, csrf_token, expires_at) VALUES (:token, :user, 'csrf', now() + interval '1 day')"),
            {"token": auth._hash(token), "user": user_id},
        )
        conn.execute(text("""
            INSERT INTO metric_definitions(code, display_name, measurement_kind, canonical_unit, value_type)
            VALUES ('solar_generation_energy', 'Solar generation', 'energy', 'kWh', 'number'),
                   ('grid_import_energy', 'Grid import', 'energy', 'kWh', 'number'),
                   ('grid_export_energy', 'Grid export', 'energy', 'kWh', 'number')
            ON CONFLICT (code) DO NOTHING
        """))
        source_ids = {}
        for code, provider, source_type in (
            ("ecoflow_powerocean", "ecoflow", "inverter"),
            ("octopus_import", "octopus", "settlement_meter"),
            ("octopus_export", "octopus", "settlement_meter"),
            ("octopus_gas", "octopus", "gas_meter"),
        ):
            source_ids[code] = conn.execute(text("""
                INSERT INTO data_sources(dashboard_id, code, provider, source_type, display_name)
                VALUES (:dashboard, :code, :provider, :source_type, :code)
                RETURNING id
            """), {"dashboard": dashboard_id, "code": code, "provider": provider, "source_type": source_type}).scalar_one()
        for source, metric, start, end, value, quality in (
            ("ecoflow_powerocean", "solar_generation_energy", "2026-10-01T00:00:00Z", "2026-10-01T01:00:00Z", 1.2, "reported"),
            ("octopus_import", "grid_import_energy", "2026-10-01T00:00:00Z", "2026-10-01T00:30:00Z", 0.4, "settled"),
            ("octopus_export", "grid_export_energy", "2026-10-01T00:00:00Z", "2026-10-01T00:30:00Z", 0.1, "settled"),
            ("octopus_import", "grid_import_energy", "2026-10-01T00:30:00Z", "2026-10-01T01:00:00Z", 0.5, "reported"),
            ("octopus_gas", "grid_import_energy", "2026-10-01T00:00:00Z", "2026-10-01T00:30:00Z", 0.6, "settled"),
        ):
            conn.execute(text("""
                INSERT INTO energy_intervals(source_id, metric_code, interval_start, interval_end, value_kwh, quality, completeness)
                VALUES (:source_id, :metric, :start, :end, :value, :quality, 'complete')
            """), {"source_id": source_ids[source], "metric": metric, "start": start, "end": end, "value": value, "quality": quality})

    with TestClient(app) as client:
        client.cookies.set(auth.SESSION_COOKIE, token)
        response = client.get("/api/exports/energy.csv")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(StringIO(response.text)))
    assert [(row["source"], row["metric"], row["resolution_minutes"], row["value_kwh"]) for row in rows] == [
        ("octopus_export", "grid_export_energy", "30", "0.100000"),
        ("octopus_import", "grid_import_energy", "30", "0.400000"),
        ("ecoflow_powerocean", "solar_generation_energy", "60", "1.200000"),
    ]
    with postgres_engine.begin() as conn:
        conn.execute(text("""
            DELETE FROM energy_intervals
            WHERE source_id IN (SELECT id FROM data_sources WHERE dashboard_id=:dashboard)
        """), {"dashboard": dashboard_id})
        conn.execute(text("DELETE FROM dashboard_accounts WHERE id=:dashboard"), {"dashboard": dashboard_id})
        conn.execute(text("DELETE FROM users WHERE id=:user"), {"user": user_id})
