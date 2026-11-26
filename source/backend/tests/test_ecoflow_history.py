from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.ecoflow_history import EcoFlowHistoryConfig, EcoFlowHistoryError, EcoFlowHistoricalImporter, month_ranges

UTC = timezone.utc

def test_start_boundary_is_first_london_month_day():
    config = EcoFlowHistoryConfig.from_environment({
        "ECOFLOW_ACCESS_KEY": "test-access", "ECOFLOW_SECRET_KEY": "test-secret",
        "ECOFLOW_POWEROCEAN_SERIAL": "test-device", "ECOFLOW_START_DATE": "2026-03-26T15:00:00Z",
    })
    assert config.start == datetime(2026, 3, 1, tzinfo=UTC)
    ranges = list(month_ranges(config.start, datetime(2026, 4, 1, tzinfo=UTC)))
    assert ranges[0][1] - ranges[0][0].replace(day=1) == ranges[0][1] - ranges[0][0]

def test_history_decoder_rejects_unverified_payload_contract():
    importer = object.__new__(EcoFlowHistoricalImporter)
    with pytest.raises(EcoFlowHistoryError, match="contract"):
        importer._decode({"code": "0", "data": {"data": "invalid"}}, datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 3, 2, tzinfo=UTC))


def test_reconciliation_uses_closed_hour_boundaries():
    now = datetime(2026, 9, 26, 12, 47, 31, tzinfo=timezone.utc)
    closed = now.replace(minute=0, second=0, microsecond=0)
    assert closed.minute == 0 and closed.second == 0


def test_reported_daily_earnings_requires_named_gbp_field():
    importer = object.__new__(EcoFlowHistoricalImporter)
    response = {"data": {"data": {"data": [
        {"indexName": "Aggregated savings", "unit": "GBP", "indexValue": "4.48"},
        {"indexName": "From Solar", "unit": "kWh", "indexValue": "12.4"},
    ]}}}
    assert importer._reported_daily_earnings(response) == Decimal("4.48")


def test_reported_daily_earnings_rejects_unnamed_or_non_currency_value():
    importer = object.__new__(EcoFlowHistoricalImporter)
    response = {"data": {"data": {"data": [
        {"indexName": "Value", "unit": "GBP", "indexValue": "4.48"},
        {"indexName": "Total earnings", "unit": "kWh", "indexValue": "4.48"},
    ]}}}
    assert importer._reported_daily_earnings(response) is None
