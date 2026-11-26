"""Read-only dashboard projections from normalized energy data."""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
import os
import json
from zoneinfo import ZoneInfo
from sqlalchemy import text
from app.database import get_engine
from app import finance
from app.tenant import current_dashboard, reset_dashboard, set_dashboard
LONDON=ZoneInfo('Europe/London')

def _dashboard_system_start() -> date | None:
    with get_engine().connect() as connection:
        value = connection.execute(text("""SELECT system_start_at FROM dashboard_settings
          WHERE dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid""")).scalar()
    return value.astimezone(LONDON).date() if value is not None else None

def bounds(day: date, period: str='day'):
    start=datetime.combine(day,time.min,LONDON)
    if period=='week': start-=timedelta(days=start.weekday()); end=start+timedelta(days=7)
    elif period=='month': start=start.replace(day=1); end=(start.replace(day=28)+timedelta(days=4)).replace(day=1)
    else: end=start+timedelta(days=1)
    return start.astimezone(timezone.utc),end.astimezone(timezone.utc)

def _rows(start,end,bucket=False,hourly=False):
    group="date_trunc('hour', interval_start)" if bucket else "NULL::timestamptz"
    duration = "AND ei.interval_end - ei.interval_start <= interval '1 hour'" if hourly else "AND ei.interval_end - ei.interval_start > interval '1 hour'"
    sql=text(f'''SELECT {group} bucket, ds.code, ei.metric_code, sum(ei.value_kwh) value
    FROM energy_intervals ei JOIN data_sources ds ON ds.id=ei.source_id
    WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
      AND ei.interval_start >= :start AND ei.interval_start < :end {duration}
    GROUP BY 1,2,3 ORDER BY 1,2,3''')
    with get_engine().connect() as c:return [dict(r._mapping) for r in c.execute(sql,{'start':start,'end':end})]

def _live_grid_energy(day: date) -> dict[str, str]:
    """Integrate current-day PowerOcean grid power between persisted snapshots.

    MQTT snapshots are ten seconds apart.  Intervals with a gap over 30 seconds
    are omitted so reconnects cannot fabricate energy.  This fills the current
    provider-reporting gap; the scheduled reported history reconciles it later.
    """
    start, end = bounds(day)
    sql = text("""SELECT ts.sampled_at, ts.value_number
        FROM telemetry_samples ts
        JOIN data_sources ds ON ds.id = ts.source_id
        WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
          AND ds.code = 'ecoflow_powerocean' AND ts.metric_code = 'grid_power'
          AND ts.sampled_at >= :start AND ts.sampled_at < :end
        ORDER BY ts.sampled_at""")
    with get_engine().connect() as connection:
        rows = connection.execute(sql, {"start": start, "end": end}).all()
    imported = Decimal("0")
    exported = Decimal("0")
    previous_at = previous_power = None
    for sampled_at, value in rows:
        if value is None:
            continue
        power = Decimal(str(value))
        if previous_at is not None:
            seconds = Decimal(str((sampled_at - previous_at).total_seconds()))
            if Decimal("0") < seconds <= Decimal("30"):
                kwh = abs(previous_power) * seconds / Decimal("3600000")
                if previous_power >= 0:
                    imported += kwh
                else:
                    exported += kwh
        previous_at, previous_power = sampled_at, power
    result: dict[str, str] = {}
    if previous_at is not None:
        result["ecoflow_powerocean:grid_import_energy"] = str(imported)
        result["ecoflow_powerocean:grid_export_energy"] = str(exported)
    return result

def actual_energy_savings(start: datetime, end: datetime, *, rows=None) -> dict[str, str | None]:
    if rows is None:
        rows = finance.daily_values(start,end)
    else:
        first, last = start.astimezone(LONDON).date(), (end-timedelta(microseconds=1)).astimezone(LONDON).date()
        rows = [row for row in rows if first <= date.fromisoformat(row["date"]) <= last]
    # The combined solar-and-battery benefit is represented by energy supplied
    # to the home.  Charging/discharging flows are internal transfers and must
    # not be added again, otherwise the benefit is overstated.
    home = finance.aggregate(rows,"ecoflow_actual")
    return {"solar_gbp":finance.money(home),"battery_gbp":None,
        "export_gbp":finance.money(finance.aggregate(rows,"electricity_export_actual"))}



def annual_projection(day: date) -> dict[str, str | int | bool | None] | None:
    """Compare fiscal-year production against the configured annual projection."""
    configured = _dashboard_system_start()
    if configured is None:
        return None
    fiscal_start_year = day.year if day.month >= configured.month else day.year - 1
    with get_engine().connect() as connection:
        entry = connection.execute(text("""SELECT generation_kwh,solar_gbp,battery_gbp,export_gbp,cost_gbp,net_position_gbp
          FROM projection_targets WHERE dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
          AND fiscal_year=:year"""), {"year": fiscal_start_year}).mappings().first()
    if entry is None:
        return None
    fiscal_start = datetime(fiscal_start_year, configured.month, 1, tzinfo=LONDON).astimezone(timezone.utc)
    fiscal_end = datetime(fiscal_start_year + 1, configured.month, 1, tzinfo=LONDON).astimezone(timezone.utc)
    through = min(datetime.now(timezone.utc), fiscal_end)
    sql = text("""SELECT coalesce(sum(ei.value_kwh), 0) FROM energy_intervals ei
      JOIN data_sources ds ON ds.id=ei.source_id
      WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
        AND ds.code='ecoflow_powerocean' AND ei.metric_code='solar_generation_energy'
        AND ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
        AND ei.interval_start >= :start AND ei.interval_start < :end
        AND ei.interval_end - ei.interval_start > interval '1 hour'""")
    with get_engine().connect() as connection:
        actual = Decimal(str(connection.execute(sql, {"start": fiscal_start, "end": through}).scalar_one()))
    target = Decimal(str(entry.generation_kwh))
    elapsed = max(Decimal("0"), Decimal(str((through - fiscal_start).total_seconds())) / Decimal(str((fiscal_end - fiscal_start).total_seconds())))
    forecast = actual / elapsed if elapsed else Decimal("0")
    system_start = finance.midnight(configured)
    savings_rows = finance.daily_values(finance.midnight(configured.replace(day=1)), fiscal_end)
    actual_savings = actual_energy_savings(max(fiscal_start,system_start), through, rows=savings_rows)
    actual_benefit = finance.available_sum(Decimal(value) if value is not None else None for value in actual_savings.values())
    # System position is cumulative.  In year two it includes every actual
    # benefit earned in year one and year two, rather than only this fiscal
    # year's benefit.  Keep the normal signed position convention: a system
    # that has not repaid its installation cost remains negative.
    system_start = datetime.combine(configured, time.min, LONDON).astimezone(timezone.utc)
    actual_system_savings = actual_energy_savings(system_start, through, rows=savings_rows)
    actual_system_benefit = finance.available_sum(Decimal(value) if value is not None else None for value in actual_system_savings.values())
    with get_engine().connect() as connection:
        system_cost = Decimal(str(connection.execute(text("""SELECT coalesce(sum(cost_gbp), 0)
          FROM projection_targets WHERE dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
          AND fiscal_year <= :year"""), {"year": fiscal_start_year}).scalar_one()))
    benefit_target = sum((Decimal(str(entry[key])) for key in ("solar_gbp", "battery_gbp", "export_gbp")), Decimal("0"))
    return {
        "year": fiscal_start_year,
        "label": f"{fiscal_start.astimezone(LONDON):%m/%Y} – {(fiscal_end - timedelta(days=1)).astimezone(LONDON):%m/%Y}",
        "actual_kwh": str(actual), "target_kwh": str(target), "forecast_kwh": str(forecast),
        "progress_percent": str(min(Decimal("100"), actual / target * Decimal("100"))) if target else None,
        "on_track": forecast >= target if target else None,
        "target_benefit_gbp": str(benefit_target),
        "actual_benefit_gbp": finance.money(actual_benefit),
        "benefit_progress_percent": str(min(Decimal("100"), actual_benefit / benefit_target * Decimal("100"))) if benefit_target and actual_benefit is not None else None,
        "target_savings": {
            "solar_gbp": str(Decimal(str(entry.solar_gbp))),
            "battery_gbp": str(Decimal(str(entry.battery_gbp))),
            "export_gbp": str(Decimal(str(entry.export_gbp))),
        },
        "actual_savings": actual_savings,
        "target_system_cost_gbp": str(entry.cost_gbp) if entry.cost_gbp is not None else None,
        "target_system_net_position_gbp": str(entry.net_position_gbp) if entry.net_position_gbp is not None else None,
        "actual_system_benefit_gbp": finance.money(actual_system_benefit),
        "actual_system_net_position_gbp": finance.money(actual_system_benefit - system_cost) if actual_system_benefit is not None else None,
        # Measure the actual position along this year's projected movement:
        # start at -system_cost and finish at the installer-projected net
        # position.  Repayment against the entire capital cost would make a
        # first-year projection bar misleadingly close to zero.
        "system_position_progress_percent": str(min(Decimal("100"), max(Decimal("0"),
            ((actual_system_benefit - system_cost) + system_cost)
            / (Decimal(str(entry.net_position_gbp)) + system_cost) * Decimal("100"))))
            if actual_system_benefit is not None and entry.net_position_gbp is not None and Decimal(str(entry.net_position_gbp)) + system_cost != 0 else None,
    }

def generated_energy_value(start: datetime, end: datetime) -> str | None:
    return finance.money(finance.aggregate(finance.daily_values(start,end),"ecoflow_generation_actual"))


def ecoflow_solar_periods(day: date) -> dict[str, str | None]:
    """Read the provider's range summaries collected from EcoFlow."""
    configured = _dashboard_system_start()
    if configured is None:
        return {"day_kwh": None, "month_kwh": None, "year_kwh": None, "lifetime_kwh": None, "year_label": None}
    year_start = date(day.year if day.month >= configured.month else day.year - 1, configured.month, 1)
    year_end = date(year_start.year + 1, configured.month, 1)
    sql = text("""SELECT tl.metric_code, tl.value_number
      FROM telemetry_latest tl JOIN data_sources ds ON ds.id = tl.source_id
      WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
        AND ds.code = 'ecoflow_powerocean' AND tl.metric_code IN (
        'ecoflow_solar_day_kwh', 'ecoflow_solar_month_kwh',
        'ecoflow_solar_year_kwh', 'ecoflow_solar_lifetime_kwh')""")
    with get_engine().connect() as connection:
        totals = {row.metric_code: str(row.value_number) for row in connection.execute(sql).mappings() if row.value_number is not None}
    month_start, month_end = bounds(day.replace(day=1), "month")
    fiscal_start = datetime.combine(year_start, time.min, LONDON).astimezone(timezone.utc)
    fiscal_end = datetime.combine(year_end, time.min, LONDON).astimezone(timezone.utc)
    lifetime_start = datetime.combine(configured, time.min, LONDON).astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    through_year = min(fiscal_end, now)
    month_through = min(month_end, now)
    # All three cards draw from the same daily valuation.  Previously this
    # function recalculated that tariff-heavy query separately for each
    # month/year/lifetime amount (and twice for its two earnings flavours).
    # Fetch it once for the widest required period, then sum the small result
    # set in Python. This keeps the totals identical while removing repeated
    # scans and tariff range lookups from the overview request.
    valuation_rows = finance.daily_values(finance.midnight(min(lifetime_start,fiscal_start,month_start).astimezone(LONDON).date().replace(day=1)),finance.midnight(now.astimezone(LONDON).date()+timedelta(days=1)))

    def earnings_total(start: datetime, end: datetime, key: str) -> str | None:
        first = start.astimezone(LONDON).date()
        last = (end - timedelta(microseconds=1)).astimezone(LONDON).date()
        period = [row for row in valuation_rows if max(first,configured) <= date.fromisoformat(row["date"]) <= last]
        if key == "calendar_octopus_earnings_gbp":
            export = finance.aggregate(period,"electricity_export_actual")
            return finance.money(finance.available_sum((
                export,
                finance.aggregate(period,"ecoflow_actual"),
            ))) if export is not None else None
        value = finance.available_sum(row[key] for row in period)
        return finance.money(value if value is not None else Decimal(0))

    def generated_total(start, end):
        first, last = start.astimezone(LONDON).date(), (end-timedelta(microseconds=1)).astimezone(LONDON).date()
        return finance.money(finance.aggregate([row for row in valuation_rows
            if first <= date.fromisoformat(row["date"]) <= last], "ecoflow_generation_actual"))

    year_generated_value = generated_total(fiscal_start, through_year)
    year_earnings = earnings_total(fiscal_start, through_year, "earnings_gbp")
    year_octopus_earnings = earnings_total(fiscal_start, through_year, "octopus_earnings_gbp")
    month_earnings = earnings_total(month_start, month_through, "earnings_gbp")
    month_octopus_earnings = earnings_total(month_start, month_through, "octopus_earnings_gbp")
    lifetime_earnings = earnings_total(lifetime_start, now, "earnings_gbp")
    lifetime_octopus_earnings = earnings_total(lifetime_start, now, "octopus_earnings_gbp")
    return {
        "day_kwh": totals.get("ecoflow_solar_day_kwh"),
        "month_kwh": totals.get("ecoflow_solar_month_kwh"),
        "year_kwh": totals.get("ecoflow_solar_year_kwh"),
        "lifetime_kwh": totals.get("ecoflow_solar_lifetime_kwh"),
        "month_gbp": generated_total(month_start, month_through),
        "year_gbp": year_generated_value,
        "lifetime_gbp": year_generated_value if lifetime_start == fiscal_start else generated_total(lifetime_start, now),
        "month_total_earnings_gbp": month_earnings,
        "year_total_earnings_gbp": year_earnings,
        "lifetime_total_earnings_gbp": year_earnings if lifetime_start == fiscal_start else lifetime_earnings,
        "month_octopus_total_earnings_gbp": month_octopus_earnings,
        "year_octopus_total_earnings_gbp": year_octopus_earnings,
        "lifetime_octopus_total_earnings_gbp": year_octopus_earnings if lifetime_start == fiscal_start else lifetime_octopus_earnings,
        "month_earnings_has_override": str(range_has_earnings_override(month_start, month_through)).lower(),
        "year_earnings_has_override": str(range_has_earnings_override(fiscal_start, through_year)).lower(),
        "lifetime_calendar_earnings_gbp": earnings_total(lifetime_start, now, "calendar_earnings_gbp"),
        "lifetime_calendar_octopus_earnings_gbp": earnings_total(lifetime_start, now, "calendar_octopus_earnings_gbp"),
        "lifetime_earnings_has_override": str(range_has_earnings_override(lifetime_start, now)).lower(),
        "year_label": f"{year_start:%m/%Y} – {(year_end - timedelta(days=1)):%m/%Y}",
    }


def fiscal_start_month() -> int:
    """Return the fiscal calendar month configured for this dashboard."""
    configured = _dashboard_system_start()
    return configured.month if configured is not None else 1

def total_earnings(day: date) -> str | None:
    """Return EcoFlow earnings: solar used at home plus export credit."""
    rows = daily_earnings(day, day)
    return rows[0]["earnings_gbp"] if rows else None

def octopus_total_earnings_for_range(start: datetime, end: datetime) -> str | None:
    """Sum the same daily Octopus values shown in the Earnings calendar."""
    return total_earnings_for_range(start, end, include_overrides=False)


def range_has_earnings_override(start: datetime, end: datetime) -> bool:
    return False


def total_earnings_for_range(start: datetime, end: datetime, *, include_overrides: bool = True) -> str | None:
    """Sum the daily configured-rate or settled-Octopus valuations."""
    rows = daily_earnings(start.astimezone(LONDON).date(), (end - timedelta(microseconds=1)).astimezone(LONDON).date())
    key = "earnings_gbp" if include_overrides else "octopus_earnings_gbp"
    return finance.money(finance.available_sum(Decimal(row[key]) if row[key] is not None else None for row in rows))

def daily_earnings(start_day: date, end_day: date) -> list[dict]:
    configured = _dashboard_system_start()
    if configured is not None:
        start_day = max(start_day,configured)
    rows = finance.daily_values(finance.midnight(start_day),finance.midnight(end_day+timedelta(days=1)))
    incomplete = octopus_settlement_incomplete_months(start_day, end_day + timedelta(days=1))
    return [{"date":row["date"],"earnings_gbp":finance.money(row["calendar_earnings_gbp"]),
        "octopus_earnings_gbp":finance.money(row["calendar_octopus_earnings_gbp"]),
        "home_gbp":finance.money(row["ecoflow_actual"]),"settled_export_gbp":finance.money(row["electricity_export_actual"]),
        "octopus_incomplete":incomplete.get(row["date"][:7], False), "has_override":False,"is_reported":False} for row in rows]



def octopus_identifiers() -> dict[str, str]:
    codes = ("ecoflow_octopus_import_mpan", "ecoflow_octopus_export_mpan", "ecoflow_octopus_gas_mprn")
    sql = text("""SELECT tl.metric_code, tl.value_text FROM telemetry_latest tl
      JOIN data_sources ds ON ds.id=tl.source_id
      WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
        AND tl.metric_code = ANY(:codes) AND tl.value_text IS NOT NULL""")
    with get_engine().connect() as connection:
        return {row.metric_code: row.value_text for row in connection.execute(sql, {"codes": list(codes)}).mappings()}

def ecoflow_diagnostics() -> list[dict[str, str]]:
    """Live diagnostic codes observed after this feature was enabled."""
    codes = ("ecoflow_pcs_ac_error_code", "ecoflow_pcs_dc_error_code", "ecoflow_pcs_ac_warning_code", "ecoflow_mppt1_fault_code", "ecoflow_mppt2_fault_code", "ecoflow_mppt1_warning_code", "ecoflow_mppt2_warning_code", "ecoflow_battery_line_off", "ecoflow_battery_relay_fault", "ecoflow_afci_fault_ch1", "ecoflow_afci_fault_ch2")
    with get_engine().connect() as connection:
        rows = connection.execute(text("""SELECT metric_code, coalesce(value_number::text, value_text, value_boolean::text) AS value,
          min(sampled_at) AS first_seen, max(sampled_at) AS last_seen
          FROM telemetry_samples ts JOIN data_sources ds ON ds.id=ts.source_id
          WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
            AND ds.code='ecoflow_powerocean' AND metric_code = ANY(:codes)
            AND coalesce(value_number, 0) <> 0
          GROUP BY metric_code, coalesce(value_number::text, value_text, value_boolean::text)
          ORDER BY max(sampled_at) DESC LIMIT 30"""), {"codes": list(codes)}).mappings().all()
    labels = {code.removeprefix("ecoflow_"): code.removeprefix("ecoflow_").replace("_", " ").title() for code in codes}
    return [{"label": labels[row.metric_code.removeprefix("ecoflow_")], "value": str(row.value), "first_seen": row.first_seen.isoformat(), "last_seen": row.last_seen.isoformat(), "severity": "Warning" if "warning" in row.metric_code else "Error"} for row in rows]

def energy_independence(day: date) -> str | None:
    """Compute EcoFlow self-sufficiency from its reported home and grid energy.

    Prefer completed hourly grid-import records because the daily provider
    summary rounds its live cumulative total.  The home total remains the
    provider's authoritative current-day total.
    """
    start, end = bounds(day)
    sql = text("""WITH day_energy AS (
      SELECT ei.metric_code, sum(ei.value_kwh) AS value
      FROM energy_intervals ei JOIN data_sources ds ON ds.id=ei.source_id
      WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
        AND ds.code='ecoflow_powerocean' AND ei.interval_start >= :start AND ei.interval_start < :end
        AND ei.interval_end - ei.interval_start > interval '1 hour'
        AND ei.metric_code IN ('ecoflow_to_home', 'home_consumption_energy')
      GROUP BY ei.metric_code
    ), hourly_grid AS (
      SELECT sum(ei.value_kwh) AS value, count(*) AS intervals
      FROM energy_intervals ei JOIN data_sources ds ON ds.id=ei.source_id
      WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
        AND ds.code='ecoflow_powerocean' AND ei.metric_code='grid_import_energy'
        AND ei.interval_start >= :start AND ei.interval_start < :end
        AND ei.interval_end - ei.interval_start <= interval '1 hour'
    ), day_grid AS (
      SELECT sum(ei.value_kwh) AS value FROM energy_intervals ei JOIN data_sources ds ON ds.id=ei.source_id
      WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
        AND ds.code='ecoflow_powerocean' AND ei.metric_code='grid_import_energy'
        AND ei.interval_start >= :start AND ei.interval_start < :end
        AND ei.interval_end - ei.interval_start > interval '1 hour'
    ) SELECT coalesce((SELECT value FROM day_energy WHERE metric_code='home_consumption_energy'),
                       (SELECT value FROM day_energy WHERE metric_code='ecoflow_to_home')) AS home,
      coalesce(nullif((SELECT value FROM hourly_grid WHERE intervals > 0), 0), (SELECT value FROM day_grid)) AS grid_import""")
    with get_engine().connect() as connection:
        row = connection.execute(sql, {"start": start, "end": end}).mappings().one()
    if row.home is None or row.grid_import is None or Decimal(str(row.home)) <= 0:
        return None
    return str(max(Decimal("0"), min(Decimal("100"), (Decimal("1") - Decimal(str(row.grid_import)) / Decimal(str(row.home))) * Decimal("100"))))


def overview(day, *, include_details: bool = True):
    start,end=bounds(day); values={}
    # Keep the daily EcoFlow fallback, then overlay the half-hourly records.
    # This supplies settled Octopus import, export, and gas values for every
    # historical date while using the more granular EcoFlow history when it is
    # available.
    for r in _rows(start,end):
        values[f"{r['code']}:{r['metric_code']}"]=str(r['value'])
    # EcoFlow's current-day response is a cumulative live summary.  Hourly
    # history is incomplete until the hour closes, so it must not replace that
    # authoritative day total.  For completed historical days, hourly values
    # remain the more detailed source.
    if day < datetime.now(LONDON).date():
        for r in _rows(start,end,hourly=True):
            values[f"{r['code']}:{r['metric_code']}"]=str(r['value'])
    # Provider-reported current-hour values are reconciled by the collector every 30 minutes.
    live={}
    with get_engine().connect() as c:
      rows=c.execute(text('''SELECT ds.code source, tl.metric_code, tl.value_number, tl.value_text, tl.value_boolean, tl.sampled_at
        FROM telemetry_latest tl JOIN data_sources ds ON ds.id=tl.source_id
        WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid''')).mappings()
      now=datetime.now(timezone.utc)
      for r in rows:
       if (now-r['sampled_at']).total_seconds()<=60:
        live[f"{r['source']}:{r['metric_code']}"]={'value':str(r['value_number']) if r['value_number'] is not None else r['value_text'] if r['value_text'] is not None else r['value_boolean'],'sampled_at':r['sampled_at'].isoformat()}
    # The first paint needs only current-day readings and the latest live
    # telemetry. Fiscal totals invoke several tariff-priced range aggregates,
    # so keep them in the deferred detail response.
    result = {'date':day.isoformat(),'start':start.isoformat(),'end':end.isoformat(),'energy':values,'live':live}
    if include_details:
        # These calculations are independent once the core readings have
        # loaded. Run them concurrently so response time is governed by the
        # slowest panel rather than the sum of all panel queries. Context
        # variables do not cross thread boundaries, so each worker restores
        # the dashboard scope before obtaining a pooled connection.
        dashboard_id = current_dashboard()

        def scoped(callable_):
            token = set_dashboard(dashboard_id) if dashboard_id is not None else None
            try:
                return callable_()
            finally:
                if token is not None:
                    reset_dashboard(token)

        detail_calls = {
            'periods': lambda: ecoflow_solar_periods(day),
            'total_earnings_gbp': lambda: total_earnings(day),
            'energy_independence_percent': lambda: energy_independence(day),
            'projection': lambda: annual_projection(day),
            'net_position': lambda: net_position(day),
            'identifiers': octopus_identifiers,
            'diagnostics': ecoflow_diagnostics,
        }
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix='overview-detail') as executor:
            futures = {key: executor.submit(scoped, call) for key, call in detail_calls.items()}
            result.update({key: future.result() for key, future in futures.items()})
    return result


def overview_details(day: date, panel: str) -> dict:
    """Return one independently-renderable overview detail panel."""
    if panel == "today":
        return {
            "periods": ecoflow_solar_periods(day),
            "total_earnings_gbp": total_earnings(day),
            "energy_independence_percent": energy_independence(day),
        }
    if panel == "projection":
        return {"projection": annual_projection(day)}
    if panel == "net-position":
        return {"net_position": net_position(day), "identifiers": octopus_identifiers()}
    raise ValueError(f"unknown overview detail panel: {panel}")

def history(day,period):
    start, end = bounds(day, period)
    # Keep every reported short interval, even where a provider has not yet
    # supplied a complete day.  A long EcoFlow interval is retained only for a
    # local day/source/metric that has no hourly data, so it is a fallback
    # rather than a duplicate daily bar.
    daily = _rows(start, end, True)
    hourly = _rows(start, end, True, hourly=True)
    hourly_days = {
        (row["code"], row["metric_code"], row["bucket"].astimezone(LONDON).date())
        for row in hourly
    }
    rows = [
        row for row in daily
        if (row["code"], row["metric_code"], row["bucket"].astimezone(LONDON).date()) not in hourly_days
    ] + hourly
    data = [{"at": row["bucket"].isoformat(), "source": row["code"], "metric": row["metric_code"], "value": str(row["value"])} for row in rows]
    return {"period": period, "start": start.isoformat(), "end": end.isoformat(), "series": data}




def costs_range(start: datetime, end: datetime) -> dict[str, str | None]:
    rows = finance.daily_values(start,end)
    def total(key): return finance.aggregate(rows,key)
    settled_export = total("electricity_export_actual")
    ecoflow_export = total("ecoflow_export_actual")
    export_fallback = settled_export is None and ecoflow_export is not None
    return {"import_cost_gbp":finance.money(finance.available_sum((total("electricity_actual"),total("electricity_standing")))),
        "export_credit_gbp":finance.money(settled_export if settled_export is not None else ecoflow_export),
        "gas_cost_gbp":finance.money(finance.available_sum((total("gas_actual"),total("gas_standing")))),
        "export_fallback":str(export_fallback).lower(), "net_fallback":str(export_fallback).lower()}



def octopus_rates(day: date) -> dict[str, object]:
    """Selected-day Octopus unit rates and standing charges for the rates panel."""
    start, end = bounds(day)
    definitions = (
        ("import", "Electricity import", "electricity", "import"),
        ("export", "Electricity export", "electricity", "export"),
        ("gas", "Gas", "gas", "import"),
    )
    result = []
    sql = text("""SELECT value_inc_vat_pence, valid_from, valid_to, tariff_code, origin
      FROM tariff_rates WHERE dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid AND origin='octopus_api' AND fuel=:fuel AND direction=:direction AND rate_kind=:kind
        AND coalesce(origin, '') <> 'dashboard_flat_rate'
        AND valid_from < :end AND (valid_to IS NULL OR valid_to > :start)
      ORDER BY valid_from""")
    with get_engine().connect() as connection:
        for code, label, fuel, direction in definitions:
            unit_rows = connection.execute(sql, {"fuel": fuel, "direction": direction, "kind": "unit_rate", "start": start, "end": end}).mappings().all()
            rates = [{"at": max(row.valid_from, start).isoformat(), "pence": str(row.value_inc_vat_pence)} for row in unit_rows]
            standing = connection.execute(text("""SELECT value_inc_vat_pence FROM tariff_rates
              WHERE fuel=:fuel AND direction=:direction AND rate_kind='standing_charge'
                AND valid_from <= :start AND (valid_to IS NULL OR valid_to > :start)
              ORDER BY valid_from DESC LIMIT 1"""), {"fuel": fuel, "direction": direction, "start": start}).scalar_one_or_none()
            # Charts only help when the selected day actually contains more
            # than one price. A fixed agreement may have many interval rows,
            # but they all carry the same unit-rate value.
            variable = len({str(row.value_inc_vat_pence) for row in unit_rows}) > 1
            result.append({"code": code, "label": label, "unit_rates": rates, "variable": variable,
                           "standing_charge_pence": str(standing) if standing is not None else None})
    return {"date": day.isoformat(), "rates": result}


def costs(day: date, period: str) -> dict[str, str | None]:
    start,end = bounds(day,period)
    return costs_range(start,end)




def net_position(day: date) -> dict[str, dict]:
    configured = _dashboard_system_start()
    if configured is None:
        return {}
    fiscal_year = day.year if day.month >= configured.month else day.year-1
    starts = {"day":day,"month":day.replace(day=1),"fiscal_year":date(fiscal_year,configured.month,1),"lifetime":configured}
    _, month_end = bounds(day,"month")
    end = max(finance.midnight(day+timedelta(days=1)),month_end)
    rows = finance.daily_values(finance.midnight(min(starts.values()).replace(day=1)),end)
    result = {}
    for key,first in starts.items():
        through = month_end.astimezone(LONDON).date() if key=="month" else day+timedelta(days=1)
        period = [row for row in rows if first <= date.fromisoformat(row["date"]) < through]
        totals = {field:finance.aggregate(period,field) for field in ("electricity_actual","electricity_export_actual","gas_actual","electricity_standing","gas_standing")}
        result[key] = {**{field:finance.money(value) for field,value in finance.net_values(totals).items()},"ecoflow_fallback":False}
        if key != "day" and totals["electricity_actual"] is None and totals["electricity_export_actual"] is None:
            electricity = -(totals["electricity_standing"] or Decimal(0))
            result[key] = {"electricity_only_gbp":finance.money(electricity),
                "including_gas_gbp":finance.money(electricity-(totals["gas_actual"] or Decimal(0))-(totals["gas_standing"] or Decimal(0))),"ecoflow_fallback":False}
        if key == "day":
            imported = finance.aggregate(period,"ecoflow_import_actual")
            exported = finance.aggregate(period,"ecoflow_export_actual")
            value = None if imported is None and exported is None else (exported or Decimal(0)) - (imported or Decimal(0)) - (totals["electricity_standing"] or Decimal(0))
            result[key] = {"electricity_only_gbp":finance.money(value),"including_gas_gbp":None,"ecoflow_fallback":False}
    return result



def energy_bills_year(year: int) -> list[dict[str, str | bool | None]]:
    rows = finance.daily_values(finance.midnight(date(year,1,1)),finance.midnight(date(year+1,1,1)))
    keys = [prefix+suffix for prefix in ("electricity","electricity_export","gas","ecoflow")
            for suffix in ("_kwh","_actual")] + ["electricity_standing","electricity_export_standing","gas_standing"]
    incomplete = octopus_settlement_incomplete_months(date(year, 1, 1), date(year + 1, 1, 1))
    result: list[dict[str, str | bool | None]] = []
    for month in range(1, 13):
        month_rows = [row for row in rows if int(row["date"][5:7]) == month]
        totals = {key: finance.aggregate(month_rows, key) for key in keys}
        net = finance.net_values(totals)
        result.append({
            "month": date(year, month, 1).isoformat(),
            "octopus_incomplete": incomplete.get(f"{year}-{month:02}", False),
            **{key: finance.money(value) for key, value in totals.items()},
            **{key: finance.money(value) for key, value in net.items()},
        })
    return result


def octopus_settlement_incomplete_months(start_day: date, end_day: date) -> dict[str, bool]:
    """Identify months with some, but not all, applicable settled Octopus coverage.

    A missing future month is not marked.  Once any settled source has reported
    for a month, the marker remains until import, export, and gas coverage (for
    the sources configured on this dashboard) cover every day in that month.
    """
    sql = text("""WITH required(code, dataset) AS (
          VALUES ('octopus_import', 'consumption'),
                 ('octopus_export', 'export_consumption'),
                 ('octopus_gas', 'gas_consumption')
        ), sources AS (
          SELECT ds.id, required.code, required.dataset
          FROM required JOIN data_sources ds ON ds.code=required.code
          WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
            AND EXISTS (SELECT 1 FROM coverage_windows cw WHERE cw.source_id=ds.id AND cw.dataset=required.dataset)
        ), days AS (
          SELECT date_trunc('month', day)::date AS month, day::date AS day,
            (day::timestamp AT TIME ZONE 'Europe/London') AS day_start,
            ((day + interval '1 day')::timestamp AT TIME ZONE 'Europe/London') AS day_end
          FROM generate_series(CAST(:start AS date), (CAST(:end AS date) - interval '1 day')::date, interval '1 day') AS day
        ), coverage AS (
          SELECT days.month, days.day, sources.id, sources.dataset,
            EXISTS (SELECT 1 FROM coverage_windows cw
              WHERE cw.source_id=sources.id AND cw.dataset=sources.dataset
                AND cw.window_start <= days.day_start AND cw.window_end >= days.day_end
                AND cw.status='complete') AS complete,
            EXISTS (SELECT 1 FROM coverage_windows cw
              WHERE cw.source_id=sources.id AND cw.dataset=sources.dataset
                AND cw.window_start < days.day_end AND cw.window_end > days.day_start) AS seen
          FROM days CROSS JOIN sources
        )
        SELECT to_char(month, 'YYYY-MM') AS month,
          bool_or(seen) AND NOT bool_and(complete) AS incomplete
        FROM coverage GROUP BY month ORDER BY month""")
    with get_engine().connect() as connection:
        records = connection.execute(sql, {"start": start_day, "end": end_day}).mappings()
        return {str(row.month): bool(row.incomplete) for row in records}



def current_tariff_agreements() -> dict[str, dict[str, str | bool | None]]:
    """Current Octopus tariff details from the normalized imported rate history."""
    now = datetime.now(timezone.utc)
    day_start = datetime.combine(now.astimezone(LONDON).date(), time.min, LONDON).astimezone(timezone.utc)
    day_end = day_start + timedelta(days=1)
    entries = (("electricity_import", "electricity", "import"), ("electricity_export", "electricity", "export"), ("gas", "gas", "import"))
    active_sql = text("""SELECT tariff_code, valid_from, valid_to
      FROM tariff_rates WHERE dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid AND origin='octopus_api' AND fuel=:fuel AND direction=:direction AND rate_kind='unit_rate'
        AND coalesce(origin, '') <> 'dashboard_flat_rate'
      ORDER BY CASE WHEN valid_from <= :now AND (valid_to IS NULL OR valid_to > :now) THEN 0 ELSE 1 END,
               valid_from DESC LIMIT 1""")
    details_sql = text("""SELECT
      min(valid_from) FILTER (WHERE rate_kind='unit_rate') AS tariff_start,
      avg(value_inc_vat_pence) FILTER (WHERE rate_kind='unit_rate' AND valid_from < :day_end AND (valid_to IS NULL OR valid_to > :day_start)) AS current_day_average_pence,
      avg(value_inc_vat_pence) FILTER (WHERE rate_kind='unit_rate') AS historical_average_pence,
      count(DISTINCT value_inc_vat_pence) FILTER (WHERE rate_kind='unit_rate') AS distinct_unit_rates
      FROM tariff_rates WHERE dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid AND origin='octopus_api' AND fuel=:fuel AND direction=:direction AND tariff_code=:tariff_code
        AND coalesce(origin, '') <> 'dashboard_flat_rate'""")
    standing_sql = text("""SELECT value_inc_vat_pence FROM tariff_rates
      WHERE dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid AND origin='octopus_api' AND fuel=:fuel AND direction=:direction AND tariff_code=:tariff_code AND rate_kind='standing_charge'
        AND coalesce(origin, '') <> 'dashboard_flat_rate'
      ORDER BY CASE WHEN valid_from <= :now AND (valid_to IS NULL OR valid_to > :now) THEN 0 ELSE 1 END, valid_from DESC LIMIT 1""")
    result: dict[str, dict[str, str | bool | None]] = {}
    with get_engine().connect() as connection:
        for key, fuel, direction in entries:
            active = connection.execute(active_sql, {"fuel": fuel, "direction": direction, "now": now}).mappings().first()
            if active is None:
                result[key] = {"tariff_code": None, "tariff_start": None, "tariff_end": None, "standing_charge_pence": None, "current_day_average_pence": None, "historical_average_pence": None, "variable": False}
                continue
            params = {"fuel": fuel, "direction": direction, "tariff_code": active.tariff_code, "now": now, "day_start": day_start, "day_end": day_end}
            details = connection.execute(details_sql, params).mappings().one()
            standing = connection.execute(standing_sql, params).scalar()
            code = str(active.tariff_code)
            result[key] = {
                "tariff_code": code,
                "tariff_start": details.tariff_start.isoformat() if details.tariff_start is not None else None,
                "tariff_end": active.valid_to.isoformat() if active.valid_to is not None else None,
                "standing_charge_pence": str(standing) if standing is not None else None,
                "current_day_average_pence": str(details.current_day_average_pence) if details.current_day_average_pence is not None else None,
                "historical_average_pence": str(details.historical_average_pence) if details.historical_average_pence is not None else None,
                "variable": int(details.distinct_unit_rates or 0) > 1,
            }
    return result

def operations() -> dict:
    """Safe operational projections: persisted events and telemetry, never credentials or provider payloads."""
    sources = ("ecoflow_powerocean", "octopus_import", "octopus_export", "octopus_gas", "octopus_mini")
    with get_engine().connect() as connection:
        import_rows = connection.execute(text("""SELECT ds.code AS source, cp.dataset, cp.watermark, cp.last_success_at,
              run.status, run.started_at, run.completed_at, run.rows_received, run.rows_written, run.requested_start, run.requested_end, run.failure_detail,
              coalesce(cov.complete_windows, 0) complete_windows, coalesce(cov.partial_windows, 0) partial_windows, coalesce(cov.failed_windows, 0) failed_windows,
              cov.coverage_start, cov.coverage_end
            FROM data_sources ds
            LEFT JOIN sync_checkpoints cp ON cp.source_id=ds.id
            LEFT JOIN LATERAL (SELECT * FROM ingestion_runs WHERE source_id=ds.id AND dataset LIKE coalesce(cp.dataset, '') || '%' ORDER BY started_at DESC, id DESC LIMIT 1) run ON true
            LEFT JOIN LATERAL (SELECT count(*) filter (where status='complete') complete_windows, count(*) filter (where status='partial') partial_windows, count(*) filter (where status='failed') failed_windows, min(window_start) coverage_start, max(window_end) coverage_end FROM coverage_windows cw WHERE source_id=ds.id AND dataset=cp.dataset
              AND NOT EXISTS (SELECT 1 FROM coverage_windows newer WHERE newer.source_id=cw.source_id AND newer.dataset=cw.dataset
                AND newer.id<>cw.id AND newer.window_start<=cw.window_start AND newer.window_end>=cw.window_end
                AND newer.recorded_at>=cw.recorded_at AND newer.status IN ('complete', 'partial'))) cov ON true
            WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
              AND ds.code = ANY(:sources) AND (cp.dataset IS NULL OR cp.dataset <> 'ecoflow_history') ORDER BY ds.code, cp.dataset NULLS LAST"""), {"sources": list(sources)}).mappings().all()
        events = connection.execute(text("""SELECT ds.code AS source, dataset, status, rows_received, rows_written, started_at, completed_at, failure_detail
          FROM ingestion_runs ir JOIN data_sources ds ON ds.id=ir.source_id
          WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
          ORDER BY ir.started_at DESC, ir.id DESC LIMIT 60""")).mappings().all()
        received = connection.execute(text("""SELECT ds.code AS source, ts.metric_code, ts.sampled_at, ts.received_at, ts.quality,
              coalesce(ts.value_number::text, ts.value_text, ts.value_boolean::text) AS value
          FROM telemetry_samples ts JOIN data_sources ds ON ds.id=ts.source_id
          WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
            AND ds.code = ANY(:sources) ORDER BY ts.received_at DESC LIMIT 80"""), {"sources": list(sources)}).mappings().all()
        health = connection.execute(text("""SELECT ds.code AS source, lph.dataset, lph.state, lph.last_attempt_at, lph.last_success_at, lph.last_sample_at, lph.consecutive_failures, lph.reason
          FROM live_provider_health lph JOIN data_sources ds ON ds.id=lph.source_id
          WHERE ds.dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid
          ORDER BY lph.last_attempt_at DESC""")).mappings().all()
    def encode(row):
        return {key: value.isoformat() if isinstance(value, datetime) else str(value) if isinstance(value, Decimal) else value for key, value in row.items()}
    imports = [encode(row) for row in import_rows]
    now = datetime.now(timezone.utc)
    for row in imports:
        # Historical imports are scheduled every five minutes. A run that is
        # still active takes precedence over its nominal next schedule.
        if not row.get("dataset") or row["source"] == "octopus_mini":
            continue
        if row.get("status") == "running":
            row["next_run_status"] = "Running now"
            continue
        started_at = row.get("started_at")
        if started_at:
            due_at = datetime.fromisoformat(started_at).astimezone(timezone.utc) + timedelta(minutes=5)
            if due_at <= now:
                row["next_run_status"] = "Due now"
            else:
                row["next_run_at"] = due_at.isoformat()
        else:
            row["next_run_status"] = "Waiting"
    # EcoFlow has a configured installation date, so its complete range is
    # knowable. Expose unattempted days explicitly instead of inferring success
    # from a non-empty coverage table.
    with get_engine().connect() as connection:
        configured_start = connection.execute(text("""SELECT system_start_at FROM dashboard_settings
          WHERE dashboard_id = nullif(current_setting('app.dashboard_id', true), '')::uuid""")).scalar_one_or_none()
    first_day = configured_start.astimezone(LONDON).date() if configured_start else None
    if first_day:
        today = datetime.now(timezone.utc).astimezone(LONDON).date()
        for row in imports:
            if row["source"] != "ecoflow_powerocean" or row.get("dataset") not in {"ecoflow_daily_history", "ecoflow_hourly_history"}:
                continue
            last_day = today if row["dataset"] == "ecoflow_daily_history" else today - timedelta(days=1)
            expected = max(0, (last_day - first_day).days + 1)
            accounted = int(row["complete_windows"]) + int(row["partial_windows"]) + int(row["failed_windows"])
            row["expected_windows"] = expected
            row["missing_windows"] = max(0, expected - accounted)
            # A partial or failed window still needs work, as does an
            # unattempted day.  Completed windows are the only ones excluded
            # from the remaining percentage.
            row["remaining_windows"] = max(0, expected - int(row["complete_windows"]))
            row["percent_left_to_import"] = round(row["remaining_windows"] * 100 / expected, 1) if expected else 0
    return {"compose_file": os.environ.get("DASHBOARD_COMPOSE_FILE", "compose.yaml"), "imports": imports, "events": [encode(row) for row in events], "received": [encode(row) for row in received], "health": [encode(row) for row in health], "pending_persistence": 0}

def daily_net_positions(start_day: date, end_day: date) -> list[dict]:
    rows = finance.daily_values(finance.midnight(start_day),finance.midnight(end_day+timedelta(days=1)))
    incomplete = octopus_settlement_incomplete_months(start_day, end_day + timedelta(days=1))
    return [{"date":row["date"], "electricity_only_gbp":finance.money(row["electricity_only_gbp"]),
        "including_gas_gbp":finance.money(row["including_gas_gbp"]), "octopus_incomplete":incomplete.get(row["date"][:7], False), "ecoflow_fallback":False,
        **{key:finance.money(row[key]) for key in ("electricity_actual","electricity_export_actual","electricity_standing","gas_actual","gas_standing")}} for row in rows]
