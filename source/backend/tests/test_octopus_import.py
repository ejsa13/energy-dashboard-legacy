from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, update

from app.models import CoverageWindow, EnergyInterval, IngestionRun, TariffRate
from app.octopus import OctopusApiError, OctopusClient, OctopusContext, OctopusGasContext
from app.octopus_import import (
    HALF_HOUR,
    LONDON,
    OctopusHistoricalImporter,
    OctopusImportConfig,
    clipped_rate_inputs,
    day_ranges,
    month_ranges,
)

UTC = timezone.utc


class FakeOctopusClient:
    def __init__(self):
        self.calls = 0
        self.agreement = {
            "tariff_code": "E-1R-AGILE-23-12-06-A",
            "valid_from": "2026-03-01T00:00:00Z",
            "valid_to": None,
        }

    def resolve_import_context(self, **_kwargs):
        return OctopusContext(
            mpan="1234567890123",
            meter_serials=("meter-1",),
            earliest_available_at=datetime(2026, 3, 1, tzinfo=UTC),
            meter_point={},
            tariff_agreements=(self.agreement,),
        )

    def consumption(self, *, start, end, **_kwargs):
        rows = []
        cursor = start
        while cursor < end:
            rows.append(
                {
                    "interval_start": cursor.isoformat().replace("+00:00", "Z"),
                    "interval_end": (cursor + HALF_HOUR).isoformat().replace("+00:00", "Z"),
                    "consumption": "0" if cursor == start else "0.123456",
                }
            )
            cursor += HALF_HOUR
        return rows

    def resolve_gas_context(self, **_kwargs):
        return OctopusGasContext("9876543210", ("gas-meter",), datetime(2026, 3, 1, tzinfo=UTC), (self.gas_agreement,))

    @property
    def gas_agreement(self):
        return {"tariff_code": "G-1R-VAR-23-01-01-A", "valid_from": "2026-03-01T00:00:00Z", "valid_to": None}

    def gas_consumption(self, *, start, end, **_kwargs):
        return self.consumption(start=start, end=end)

    def gas_unit_rates(self, **_kwargs):
        return self.unit_rates()

    def gas_standing_charges(self, **_kwargs):
        return self.standing_charges()

    def unit_rates(self, **_kwargs):
        return [{"valid_from": "2026-03-01T00:00:00Z", "valid_to": None, "value_inc_vat": "-1.000001", "value_exc_vat": "-0.952382", "payment_method": "DIRECT_DEBIT"}]

    def standing_charges(self, **_kwargs):
        return [{"valid_from": "2026-03-01T00:00:00Z", "valid_to": None, "value_inc_vat": "50", "value_exc_vat": "47.619048", "payment_method": "DIRECT_DEBIT"}]


def octopus_env() -> dict[str, str]:
    return {
        "ECOFLOW_START_DATE": "2026-03-15T12:00:00Z",
        "OCTOPUS_API_KEY": "test-key",
        "OCTOPUS_ACCOUNT_NUMBER": "A-123",
    }


def test_config_requires_only_octopus_account_credentials():
    config = OctopusImportConfig.from_environment(octopus_env())
    assert config.account_number == "A-123"
    with pytest.raises(ValueError, match="required"):
        OctopusImportConfig.from_environment({})


def test_client_paginates_mocked_consumption_without_network():
    requests = []
    payloads = [
        b'{"results":[{"interval_start":"2026-03-01T00:00:00Z"}],"next":"https://api.octopus.energy/v1/page-2"}',
        b'{"results":[{"interval_start":"2026-03-01T00:30:00Z"}],"next":null}',
    ]

    def transport(req, _timeout):
        requests.append(req)
        return payloads.pop(0)

    client = OctopusClient("test-key", transport=transport)
    rows = client.consumption(
        mpan="1234567890123",
        meter_serial="meter-1",
        start=datetime(2026, 3, 1, tzinfo=UTC),
        end=datetime(2026, 3, 1, 1, tzinfo=UTC),
    )
    assert len(rows) == 2
    assert requests[0].get_header("Authorization", "").startswith("Basic ")
    assert "page_size=250" in requests[0].full_url


def test_client_discovers_the_single_import_meter_and_settlement_serial():
    payload = b'''{"properties":[{"electricity_meter_points":[
      {"mpan":"111","is_export":false,"agreements":[{"tariff_code":"E-1R-AGILE-23-12-06-A","valid_from":"2023-12-06T00:00:00Z"}],"meters":[{"serial_number":"old-import-meter","registers":[{"is_settlement_register":true}]},{"serial_number":"import-meter","registers":[{"is_settlement_register":true}]}]},
      {"mpan":"222","is_export":true,"agreements":[{"tariff_code":"E-1R-OUTGOING-VAR-23-01-01-A","valid_from":"2023-01-01T00:00:00Z"}],"meters":[{"serial_number":"export-meter","registers":[{"is_settlement_register":true}]}]}
    ]}]}'''
    client = OctopusClient("test-key", transport=lambda _req, _timeout: payload)
    context = client.resolve_import_context(account_number="A-123")
    assert context.mpan == "111"
    assert context.meter_serials == ("import-meter", "old-import-meter")
    assert context.earliest_available_at == datetime(2023, 12, 6, tzinfo=UTC)


def test_month_ranges_preserve_dst_boundaries():
    ranges = list(month_ranges(datetime(2026, 3, 1, tzinfo=UTC), datetime(2026, 4, 30, 23, tzinfo=UTC)))
    assert len(ranges) == 2
    assert ranges[0][1] - ranges[0][0] == timedelta(days=30, hours=23)


def test_day_ranges_preserve_dst_boundaries():
    ranges = list(day_ranges(datetime(2026, 3, 28, tzinfo=UTC), datetime(2026, 3, 31, 23, tzinfo=UTC)))
    assert [end - start for start, end in ranges] == [timedelta(days=1), timedelta(hours=23), timedelta(days=1), timedelta(days=1)]


def test_tariff_rates_are_clipped_and_keep_decimal_values():
    rates = clipped_rate_inputs(
        [{"valid_from": "2026-03-01T00:00:00Z", "valid_to": None, "value_inc_vat": "-0.123456", "value_exc_vat": "-0.117577"}],
        tariff_code="E-1R-AGILE-23-12-06-A",
        rate_kind="unit_rate",
        start=datetime(2026, 3, 15, tzinfo=UTC),
        end=datetime(2026, 4, 1, tzinfo=UTC),
        agreement_start=datetime(2026, 3, 1, tzinfo=UTC),
        agreement_end=None,
    )
    assert len(rates) == 1
    assert rates[0].valid_from == datetime(2026, 3, 15, tzinfo=UTC)
    assert rates[0].valid_to == datetime(2026, 4, 1, tzinfo=UTC)
    assert rates[0].value_inc_vat_pence == Decimal("-0.123456")


def test_importer_persists_complete_history_and_tariffs(postgres_engine, monkeypatch):
    values = octopus_env()
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    fake = FakeOctopusClient()
    end = datetime(2026, 3, 1, 1, tzinfo=UTC)
    importer = OctopusHistoricalImporter(client_factory=lambda _key: fake, now=lambda: end, sleep=lambda _seconds: None)
    importer._ranges = lambda **_kwargs: ("backfill", [(datetime(2026, 3, 1, tzinfo=UTC), end)])  # type: ignore[method-assign]
    importer._gas_ranges = lambda **_kwargs: ("backfill", [(datetime(2026, 3, 1, tzinfo=UTC), end)])  # type: ignore[method-assign]
    importer.run()
    with postgres_engine.connect() as connection:
        readings = connection.execute(select(EnergyInterval.value_kwh).order_by(EnergyInterval.interval_start)).scalars().all()
        rate_count = connection.execute(select(func.count()).select_from(TariffRate)).scalar_one()
        statuses = connection.execute(select(IngestionRun.status).order_by(IngestionRun.id)).scalars().all()
    assert readings == [Decimal("0.000000"), Decimal("0.000000"), Decimal("1.399991"), Decimal("0.123456")]
    assert rate_count == 4
    assert statuses == ["complete", "complete", "complete", "complete"]


def test_retry_is_bounded_for_transient_failures(postgres_engine):
    importer = OctopusHistoricalImporter(now=lambda: datetime(2026, 3, 1, tzinfo=UTC), sleep=lambda _seconds: None)
    attempts = 0

    def action():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OctopusApiError("temporary", transient=True)
        return "ok"

    assert importer._retry(action) == "ok"
    assert attempts == 3


def test_old_partial_coverage_retries_every_six_hours(postgres_engine):
    now = datetime(2026, 4, 1, tzinfo=UTC)
    importer = OctopusHistoricalImporter(now=lambda: now, sleep=lambda _seconds: None)
    importer.catalog.seed()
    start = now - timedelta(days=21)
    end = now - timedelta(days=20)
    importer.ingestion.record_coverage(
        source_code="octopus_import", dataset="consumption", start=start, end=end, status="partial"
    )

    with postgres_engine.begin() as connection:
        connection.execute(update(CoverageWindow).values(recorded_at=now - timedelta(hours=7)))
    assert importer._retryable_coverage(source_code="octopus_import", dataset="consumption", end=now) == [(start, end)]

    with postgres_engine.begin() as connection:
        connection.execute(update(CoverageWindow).values(recorded_at=now - timedelta(hours=5)))
    assert importer._retryable_coverage(source_code="octopus_import", dataset="consumption", end=now) == []


def test_partial_month_does_not_block_later_backfill(postgres_engine, monkeypatch):
    importer = OctopusHistoricalImporter(now=lambda: datetime(2026, 4, 1, tzinfo=UTC), sleep=lambda _s: None)
    # A partial coverage checkpoint remains only an optimisation hint; later ranges run.
    importer.ingestion.update_checkpoint(source_code="octopus_import", dataset="consumption", watermark=datetime(2026, 3, 1, tzinfo=UTC), cursor=None, succeeded_at=datetime(2026, 3, 1, tzinfo=UTC))
    mode, ranges = importer._ranges(dataset="consumption", earliest_available_at=datetime(2026, 3, 1, tzinfo=UTC), end=datetime(2026, 4, 1, tzinfo=UTC))
    assert mode == "backfill" and ranges
