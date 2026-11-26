from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import json
from types import SimpleNamespace
import uuid

import pytest
from sqlalchemy import text

from app import dashboard, finance, tenant
from app.main import _apply_evening_battery_dispatch, _tariff_comparison_value

DAY = date(2026,6,1)
START = finance.midnight(DAY)


@pytest.fixture
def ledger(postgres_engine):
    with postgres_engine.begin() as c:
        email = f"finance-{uuid.uuid4().hex}@example.com"
        owner = c.execute(text("INSERT INTO users(email,google_subject) VALUES (:email,:email) RETURNING id"), {"email":email}).scalar_one()
        did = c.execute(text("INSERT INTO dashboard_accounts(owner_user_id) VALUES (:id) RETURNING id"), {"id":owner}).scalar_one()
        config = {"import_rates":[{"start_date":str(DAY),"end_date":str(DAY),"kind":"flat","pence":"5"}],"export_rates":[{"start_date":str(DAY),"end_date":str(DAY),"kind":"flat","pence":"30"}]}
        c.execute(text("INSERT INTO dashboard_settings(dashboard_id,system_start_at,earnings_config) VALUES (:id,:start,CAST(:config AS jsonb))"), {"id":did,"start":START,"config":json.dumps(config)})
    token = tenant.set_dashboard(str(did))
    from app.repositories import CatalogRepository
    CatalogRepository(postgres_engine).seed(str(did))
    with postgres_engine.begin() as c:
        c.execute(text("INSERT INTO metric_definitions(code,display_name,measurement_kind,canonical_unit,value_type) VALUES ('ecoflow_to_home','To home','energy','kWh','number') ON CONFLICT DO NOTHING"))
        sources = dict(c.execute(text("SELECT code,id FROM data_sources WHERE dashboard_id=:id"), {"id":did}).all())
    def energy(source,metric,value,a=0,b=30,quality="settled"):
        with postgres_engine.begin() as c:
            c.execute(text("INSERT INTO energy_intervals(source_id,metric_code,interval_start,interval_end,value_kwh,quality,completeness) VALUES (:source,:metric,:a,:b,:value,:quality,'complete')"), {"source":sources[source],"metric":metric,"a":START+timedelta(minutes=a),"b":START+timedelta(minutes=b),"value":D(value),"quality":quality})
    def rate(fuel,direction,value,a=0,b=1440,kind="unit_rate",origin="octopus_api",scope=did):
        with postgres_engine.begin() as c:
            c.execute(text("INSERT INTO tariff_rates(dashboard_id,fuel,direction,tariff_code,rate_kind,valid_from,valid_to,value_inc_vat_pence,value_exc_vat_pence,origin) VALUES (:id,:fuel,:direction,:code,:kind,:a,:b,:value,:value,:origin)"), {"id":scope,"fuel":fuel,"direction":direction,"code":uuid.uuid4().hex,"kind":kind,"a":START+timedelta(minutes=a),"b":START+timedelta(minutes=b),"value":D(value),"origin":origin})
    yield did, energy, rate
    tenant.reset_dashboard(token)


def populate(ledger):
    _,energy,rate=ledger
    rate("electricity","import","20",b=30)
    rate("electricity","import","40",a=30)
    rate("electricity","export","10",b=30)
    rate("electricity","export","20",a=30)
    rate("gas","import","5")
    rate("electricity","import","50",kind="standing_charge")
    rate("gas","import","20",kind="standing_charge")
    # These must never contaminate Octopus values.
    rate("electricity","export","0.12",origin="dashboard_flat_rate")
    rate("electricity","import","999",scope=None)
    for a,b,i,e in ((0,30,"1","4"),(30,60,"3","2")):
        energy("octopus_import","grid_import_energy",i,a,b)
        energy("octopus_export","grid_export_energy",e,a,b)
    energy("octopus_gas","gas_consumption_kwh","2",0,60)
    energy("octopus_import","grid_import_energy","100",60,90,quality="estimated")
    for metric,value in (("ecoflow_to_home","10"),("battery_charge_energy","4"),("battery_discharge_energy","2"),("grid_import_energy","1"),("grid_export_energy","2")):
        energy("ecoflow_powerocean",metric,value,0,60,quality="reported")
        energy("ecoflow_powerocean",metric,value,0,1440,quality="reported")


def test_formulas_and_shared_billing_net_position(ledger):
    populate(ledger)
    rows=finance.daily_values(START,START+timedelta(days=1))
    row=rows[0]
    assert row["electricity_actual"] == D("1.4")
    assert row["electricity_export_actual"] == D("0.8")
    assert row["ecoflow_actual"] == D("3")
    assert row["ecoflow_battery_actual"] == D("1.2")
    assert row["ecoflow_export_actual"] == D("0.3")
    assert row["earnings_gbp"] == D("1.1")
    assert row["octopus_earnings_gbp"] == D("2.2")
    assert row["electricity_only_gbp"] == D("-1.1")
    assert row["including_gas_gbp"] == D("-1.4")
    projection=dashboard.actual_energy_savings(START,START+timedelta(days=1))
    assert D(projection["solar_gbp"]) == D("3")
    assert projection["battery_gbp"] is None
    assert D(projection["export_gbp"]) == D("0.8")
    assert D(dashboard.daily_earnings(DAY,DAY)[0]["octopus_earnings_gbp"]) == D("3.8")
    assert D(dashboard.daily_net_positions(DAY,DAY)[0]["including_gas_gbp"]) == D("-1.4")
    assert dashboard.net_position(DAY)["day"]["including_gas_gbp"] is None
    assert D(dashboard.net_position(DAY)["day"]["electricity_only_gbp"]) == D("-0.5")


def test_missing_rates_not_zero_or_ecoflow_fallback(ledger):
    _,energy,rate=ledger
    energy("octopus_import","grid_import_energy","1",0,60)
    rate("electricity","import","20",0,30)
    rate("electricity","export","12")
    energy("ecoflow_powerocean","grid_export_energy","100",0,60,quality="reported")
    row=finance.daily_values(START,START+timedelta(days=1))[0]
    assert row["electricity_actual"] == D("0.1")
    assert row["electricity_export_actual"] is None
    assert row["octopus_earnings_gbp"] == D("0.1")
    assert row["electricity_only_gbp"] is None
    assert row["electricity_kwh"] == D("1")


def test_rate_weighting_negative_rates_and_gaps():
    curve=finance.Rates([(START,START+timedelta(minutes=15),D("20")),(START+timedelta(minutes=15),START+timedelta(hours=1),D("40"))],START,START+timedelta(hours=1))
    assert curve.price(START,START+timedelta(hours=1),D("10")) == D("3.5")
    negative=finance.Rates([(START,None,D("-5"))],START,START+timedelta(hours=1))
    assert negative.price(START,START+timedelta(hours=1),D("2")) == D("-0.1")
    assert finance.Rates([],START,START+timedelta(hours=1)).price(START,START+timedelta(hours=1),D("0")) == 0


def test_tariff_comparison_prices_each_interval_at_its_own_rate():
    curve = finance.Rates([
        (START, START + timedelta(minutes=30), D("10")),
        (START + timedelta(minutes=30), START + timedelta(hours=1), D("40")),
    ], START, START + timedelta(hours=1))
    rows = [
        SimpleNamespace(interval_start=START, interval_end=START + timedelta(minutes=30), value_kwh=D("1.2")),
        SimpleNamespace(interval_start=START + timedelta(minutes=30), interval_end=START + timedelta(hours=1), value_kwh=D("3.4")),
    ]
    # 1.2 kWh × 10p plus 3.4 kWh × 40p; this is deliberately not an
    # average rate multiplied by a total kWh value.
    assert _tariff_comparison_value(rows, curve) == D("1.48")


def test_optional_evening_battery_dispatch_releases_capacity_percentage_over_window_and_exports_surplus():
    rows = [
        SimpleNamespace(interval_start=START + timedelta(hours=16), interval_end=START + timedelta(hours=17), value_kwh=D("1")),
        SimpleNamespace(interval_start=START + timedelta(hours=17), interval_end=START + timedelta(hours=18), value_kwh=D("4")),
        SimpleNamespace(interval_start=START + timedelta(hours=18), interval_end=START + timedelta(hours=19), value_kwh=D("0.5")),
    ]
    adjusted, simulated_export, dispatched = _apply_evening_battery_dispatch(rows, D("10"), D("50"), "17:00", "19:00")
    # 50% of 10 kWh is 5 kWh total.  It is spread evenly across 17:00–19:00.
    assert dispatched == D("5")
    assert [row.value_kwh for row in adjusted] == [D("1"), D("1.5"), D("0")]
    assert [row.value_kwh for row in simulated_export] == [D("2")]


def test_london_days_include_dst():
    assert finance.midnight(date(2026,3,30))-finance.midnight(date(2026,3,29)) == timedelta(hours=23)
    assert finance.midnight(date(2026,10,26))-finance.midnight(date(2026,10,25)) == timedelta(hours=25)


def test_month_fiscal_and_billing_totals_agree(ledger, postgres_engine):
    did,energy,rate=ledger
    for fuel,direction,value in (("electricity","import","20"),("electricity","export","12"),("gas","import","5")):
        rate(fuel,direction,value,b=43200)
    rate("electricity","import","50",b=43200,kind="standing_charge")
    rate("gas","import","20",b=43200,kind="standing_charge")
    with postgres_engine.begin() as c:
        c.execute(text("UPDATE dashboard_settings SET earnings_config=CAST(:config AS jsonb) WHERE dashboard_id=:id"), {"id":did,"config":json.dumps({"import_rates":[{"kind":"dynamic","start_date":"2026-06-01","end_date":"2026-06-30"}],"export_rates":[{"kind":"flat","pence":"30","start_date":"2026-06-01","end_date":"2026-06-30"}]})})
    for day in range(30):
        a,b=day*1440,day*1440+30
        for source,metric,value in (("octopus_import","grid_import_energy","1"),("octopus_export","grid_export_energy","2"),("octopus_gas","gas_consumption_kwh","3"),("ecoflow_powerocean","ecoflow_to_home","4"),("ecoflow_powerocean","grid_export_energy","5")):
            energy(source,metric,value,a,b,quality="reported" if source.startswith("ecoflow") else "settled")
    bills=dashboard.energy_bills_year(2026)[5]
    assert D(bills["electricity_actual"]) == D("6")
    assert D(bills["electricity_export_actual"]) == D("7.2")
    assert D(bills["electricity_standing"]) == D("15")
    assert D(bills["gas_actual"]) == D("4.5")
    assert D(bills["gas_standing"]) == D("6")
    assert D(bills["ecoflow_actual"]) == D("24")
    summary=dashboard.net_position(date(2026,6,30))
    for period in ("month","fiscal_year","lifetime"):
        assert D(summary[period]["electricity_only_gbp"]) == D("-13.8")
        assert D(summary[period]["including_gas_gbp"]) == D("-24.3")
    daily=dashboard.daily_net_positions(DAY,date(2026,6,30))
    assert sum(D(row["including_gas_gbp"]) for row in daily)==D("-24.3")
    earnings=dashboard.daily_earnings(DAY,date(2026,6,30))
    assert sum(D(row["earnings_gbp"]) for row in earnings)==D("69")
    assert sum(D(row["octopus_earnings_gbp"]) for row in earnings)==D("31.2")


def test_configured_end_date_is_inclusive_and_missing_standing_is_unknown(ledger):
    _,energy,rate=ledger
    energy("ecoflow_powerocean","ecoflow_to_home","4",1410,1440,quality="reported")
    energy("ecoflow_powerocean","grid_export_energy","2",1410,1440,quality="reported")
    energy("octopus_import","grid_import_energy","1",1410,1440)
    energy("octopus_export","grid_export_energy","2",1410,1440)
    rate("electricity","import","20")
    rate("electricity","export","12")
    row=finance.daily_values(START,START+timedelta(days=1))[0]
    assert row["earnings_gbp"] == D("0.8")
    assert row["octopus_earnings_gbp"] == D("0.44")
    assert row["electricity_only_gbp"] is None



def test_finance_cache_is_scoped_copied_and_expires(monkeypatch):
    finance.invalidate_cache()
    ticks=[0]
    calls=[]
    monkeypatch.setattr(finance,"monotonic",lambda:ticks[0])
    def calculate(start,end):
        calls.append(tenant.current_dashboard())
        return [{"tenant":tenant.current_dashboard(),"value":len(calls)}]
    monkeypatch.setattr(finance,"_calculate_daily_values",calculate)
    scope=tenant.set_dashboard("tenant-a")
    try:
        result=finance.daily_values(START,START+timedelta(days=1))
        result[0]["value"]=999
        assert finance.daily_values(START,START+timedelta(days=1))[0]["value"]==1
        other=tenant.set_dashboard("tenant-b")
        try:
            assert finance.daily_values(START,START+timedelta(days=1))[0]["tenant"]=="tenant-b"
        finally:
            tenant.reset_dashboard(other)
        ticks[0]=finance._CACHE_SECONDS+1
        assert finance.daily_values(START,START+timedelta(days=1))[0]["value"]==3
        finance.invalidate_cache()
        assert finance.daily_values(START,START+timedelta(days=1))[0]["value"]==4
    finally:
        tenant.reset_dashboard(scope)
        finance.invalidate_cache()


def test_concurrent_finance_requests_share_one_calculation(monkeypatch):
    from concurrent.futures import Future, ThreadPoolExecutor
    from threading import Event
    finance.invalidate_cache()
    entered, waiting, release=Event(),Event(),Event()
    calls=[]
    class ObservedFuture(Future):
        def result(self,timeout=None):
            waiting.set()
            return super().result(timeout)
    monkeypatch.setattr(finance,"Future",ObservedFuture)
    def calculate(start,end):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return [{"value":42}]
    monkeypatch.setattr(finance,"_calculate_daily_values",calculate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(finance.daily_values,START,START+timedelta(days=1))
        assert entered.wait(5)
        second=pool.submit(finance.daily_values,START,START+timedelta(days=1))
        try:
            assert waiting.wait(5)
        finally:
            release.set()
        assert first.result()==second.result()==[{"value":42}]
    assert len(calls)==1
    finance.invalidate_cache()


def test_overview_reuses_one_finance_valuation(ledger,monkeypatch,postgres_engine):
    from unittest.mock import Mock
    populate(ledger)
    spy=Mock(wraps=finance.daily_values)
    monkeypatch.setattr(finance,"daily_values",spy)
    dashboard.ecoflow_solar_periods(DAY)
    assert spy.call_count==1
    with postgres_engine.begin() as c:
        c.execute(text("INSERT INTO projection_targets(dashboard_id,fiscal_year,generation_kwh,solar_gbp,battery_gbp,export_gbp) VALUES (:id,2026,100,10,10,10)"), {"id":ledger[0]})
    spy.reset_mock()
    assert dashboard.annual_projection(DAY) is not None
    assert spy.call_count==1


def test_partial_period_keeps_available_values_and_unsettled_days_unknown(ledger):
    populate(ledger)
    rows = finance.daily_values(START,START+timedelta(days=3))
    assert finance.aggregate(rows,"electricity_actual") == D("1.4")
    assert finance.aggregate(rows,"ecoflow_actual") == D("3")
    assert rows[1]["calendar_octopus_earnings_gbp"] is None
    assert rows[1]["electricity_only_gbp"] is None
    assert finance.aggregate(rows[1:],"electricity_actual") is None
    assert D(dashboard.total_earnings(DAY)) == D("1.1")


def test_tariff_recovery_uses_dashboard_history_not_another_tenant(ledger):
    from app.octopus_import import OctopusHistoricalImporter
    did,_,rate = ledger
    rate("electricity","import","20",a=1440,b=2880)
    rate("electricity","import","20",a=-1440,b=2880,scope=None)
    importer=OctopusHistoricalImporter(dashboard_id=str(did))
    ranges=importer._tariff_backfill([],START,START+timedelta(days=2),"electricity","import")
    assert ranges == [(START,START+timedelta(days=1))]


def test_checkpoint_cannot_skip_unattempted_months(ledger):
    from app.octopus_import import OctopusHistoricalImporter
    did,_,_=ledger
    end=START+timedelta(days=30)
    importer=OctopusHistoricalImporter(dashboard_id=str(did))
    importer.ingestion.record_coverage(source_code="octopus_import",dataset="consumption",start=START+timedelta(days=20),end=end,status="complete")
    importer.ingestion.update_checkpoint(source_code="octopus_import",dataset="consumption",watermark=end,cursor=None,succeeded_at=end)
    _,ranges=importer._ranges(dataset="consumption",earliest_available_at=START,end=end)
    assert ranges[0][0] == START
