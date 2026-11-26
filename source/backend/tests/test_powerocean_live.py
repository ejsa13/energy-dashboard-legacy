from decimal import Decimal

from app.powerocean_live import PowerOceanLiveConfig, PowerOceanLiveError, _bytes, _pack_capacity_wh, _varint, decode_payload


def test_decode_payload_unwraps_safe_json():
    assert decode_payload(b'{"data":{"params":{"mpptPwr":123,"bpSoc":55}}}') == {"mpptPwr": 123, "bpSoc": 55}


def test_decode_payload_extracts_reported_full_battery_capacity():
    decoded = decode_payload(b'{"params":{"ems_change_report":{"poAiSchedule":{"bpFullCap":10240}}}}')
    assert decoded["battery_capacity_wh"] == 10240


def test_decode_payload_extracts_full_battery_capacity_from_latest_quotas():
    decoded = decode_payload(b'{"data":{"quotaMap":{"ems_change_report.poAiSchedule.bpFullCap":10240}}}')
    assert decoded["battery_capacity_wh"] == 10240


def test_pack_capacity_uses_reported_remaining_energy_and_soc():
    assert _pack_capacity_wh({"remain_wh": 2560, "soc": 50}) == Decimal("5120")
    assert _pack_capacity_wh({"remain_wh": 0, "soc": 0}) is None


def test_decode_payload_rejects_non_json_payloads():
    assert decode_payload(b"\x00\x01not-json") == {}


def test_status_decoder_does_not_invent_missing_status_fields():
    # A 96/8 state frame that reports only the online-pack count must not
    # manufacture absent inverter, EMS, or grid status values as None.
    data = _varint(9, 2)
    header = _bytes(1, data) + _varint(8, 96) + _varint(9, 8)
    decoded = decode_payload(_bytes(1, header))

    assert decoded == {"bp_online_sum": 2}


def test_live_configuration_requires_all_collector_values(monkeypatch):
    for key in ("ECOFLOW_APP_EMAIL", "ECOFLOW_APP_PASSWORD", "ECOFLOW_POWEROCEAN_SERIAL"):
        monkeypatch.delenv(key, raising=False)
    try:
        PowerOceanLiveConfig.from_environment()
    except PowerOceanLiveError as error:
        assert str(error) == "Missing required EcoFlow live configuration"
    else:
        raise AssertionError("configuration without collector secrets must fail")


def test_live_configuration_uses_supplied_dashboard_configuration():
    config = PowerOceanLiveConfig.from_environment({
        "ECOFLOW_APP_EMAIL": "one@example.test",
        "ECOFLOW_APP_PASSWORD": "secret",
        "ECOFLOW_POWEROCEAN_SERIAL": "INVERTER-ONE",
        "ECOFLOW_BATTERY_SERIALS": "BATTERY-ONE, BATTERY-TWO",
    })

    assert config.email == "one@example.test"
    assert config.serial == "INVERTER-ONE"
    assert config.battery_serials == ("BATTERY-ONE", "BATTERY-TWO")
