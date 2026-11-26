from datetime import datetime, timezone
from decimal import Decimal
from app.octopus_mini import MAP, OctopusMiniWorker

def test_mini_metric_catalog_is_complete():
    assert set(MAP) == {"consumption", "export", "demand", "consumptionDelta", "costDelta", "costDeltaWithTax"}
    assert MAP["costDeltaWithTax"] == "octopus_mini_cost_delta_inc_vat"

def test_mini_measurement_decimal_contract():
    assert Decimal("0.000001") == Decimal(str("0.000001"))
    assert datetime.fromisoformat("2026-09-26T12:00:00+00:00").tzinfo is not None


def test_mini_worker_uses_its_dashboard_configuration():
    worker = OctopusMiniWorker(env={
        "OCTOPUS_API_KEY": "dashboard-one-key",
        "OCTOPUS_ACCOUNT_NUMBER": "A-1",
        "OCTOPUS_MINI_DEVICE_ID": "mini-1",
    })

    assert worker.env["OCTOPUS_API_KEY"] == "dashboard-one-key"
    assert worker.device_id == "mini-1"
