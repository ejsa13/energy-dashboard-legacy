"""Shared interval pricing for projections, earnings, bills and net position.

Octopus prices are exclusively scoped octopus_api rates in pence/kWh.
EcoFlow prices come from the dashboard's date-effective rate configuration.
Longer energy intervals are allocated uniformly across rate/day boundaries;
this is an estimate when sub-interval energy readings are unavailable.
"""
from bisect import bisect_right
from collections import defaultdict, OrderedDict
from concurrent.futures import Future
from threading import Lock
from time import monotonic
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
import heapq
from zoneinfo import ZoneInfo

from sqlalchemy import text
from app.database import get_engine
from app.tenant import current_dashboard

LONDON = ZoneInfo("Europe/London")
ZERO = Decimal("0")
HUNDRED = Decimal("100")


@lru_cache(maxsize=4096)
def midnight(day):
    return datetime.combine(day, time.min, LONDON).astimezone(timezone.utc)


def money(value):
    return str(value) if value is not None else None


def complete_sum(values):
    values = list(values)
    return sum(values, ZERO) if values and all(v is not None for v in values) else None


def available_sum(values):
    return complete_sum(v for v in values if v is not None)


class Rates:
    """Non-overlapping segments; newest applicable tariff wins deterministically."""
    def __init__(self, rows, start, end):
        starts = defaultdict(list)
        points = {start, end}
        for i, row in enumerate(rows):
            a, b, value = row
            a, b = max(start, a), min(end, b or end)
            if a >= b:
                continue
            starts[a].append((-row[0].timestamp(), -i, b, value))
            points.update((a, b))
        self.points = sorted(points)
        self.values = []
        active = []
        for point in self.points[:-1]:
            for entry in starts[point]:
                heapq.heappush(active, entry)
            while active and active[0][2] <= point:
                heapq.heappop(active)
            self.values.append(active[0][3] if active else None)

    def at(self, moment):
        i = bisect_right(self.points, moment) - 1
        return self.values[i] if 0 <= i < len(self.values) else None

    def price(self, a, b, kwh):
        if kwh == 0:
            return ZERO
        i = max(0, bisect_right(self.points, a)-1)
        if i < len(self.values) and b <= self.points[i+1]:
            rate = self.values[i]
            return kwh * rate / HUNDRED if rate is not None else None
        total = ZERO
        priced = False
        duration = Decimal(str((b-a).total_seconds()))
        i = max(0, bisect_right(self.points, a)-1)
        cursor = a
        while cursor < b and i < len(self.values):
            until = min(b, self.points[i+1])
            rate = self.values[i]
            if rate is not None:
                priced = True
                total += kwh * Decimal(str((until-cursor).total_seconds())) / duration * rate / HUNDRED
            cursor = until
            i += 1
        return total if priced else None


# source, metric -> (billing/valuation key, fuel, direction)
METRICS = {
    ("octopus_import", "grid_import_energy"): ("electricity", "electricity", "import"),
    ("octopus_export", "grid_export_energy"): ("electricity_export", "electricity", "export"),
    ("octopus_gas", "gas_consumption_kwh"): ("gas", "gas", "import"),
    ("ecoflow_powerocean", "ecoflow_to_home"): ("ecoflow", "electricity", "import"),
    ("ecoflow_powerocean", "grid_export_energy"): ("ecoflow_export", "electricity", "export"),
    ("ecoflow_powerocean", "solar_generation_energy"): ("ecoflow_generation", "electricity", "export"),
    ("ecoflow_powerocean", "grid_import_energy"): ("ecoflow_import", "electricity", "import"),
    ("ecoflow_powerocean", "battery_discharge_energy"): ("ecoflow_discharge", "electricity", "import"),
    ("ecoflow_powerocean", "battery_charge_energy"): ("ecoflow_battery", "electricity", "import"),
}


_CACHE_SECONDS = 15
_CACHE_ENTRIES = 32
_cache = OrderedDict()
_inflight = {}
_cache_lock = Lock()
_cache_generation = 0


def invalidate_cache():
    """Settings writes invalidate cached and in-flight results immediately."""
    global _cache_generation
    with _cache_lock:
        _cache_generation += 1
        _cache.clear()


def daily_values(start, end):
    now = datetime.now(timezone.utc)
    if start >= min(end, now):
        return []
    # Future-ended ranges share the same current snapshot. Include the local
    # day so yesterday's live snapshot is never reused across midnight.
    end_key = ("live", now.astimezone(LONDON).date()) if end >= now else end
    with _cache_lock:
        generation = _cache_generation
        key = (get_engine(), current_dashboard(), start, end_key, generation)
        cached = _cache.get(key)
        if cached is not None and cached[0] > monotonic():
            _cache.move_to_end(key)
            return [dict(row) for row in cached[1]]
        future = _inflight.get(key)
        leader = future is None
        if leader:
            future = Future()
            _inflight[key] = future
    if not leader:
        return [dict(row) for row in future.result()]
    try:
        rows = _calculate_daily_values(start, min(end, now))
        with _cache_lock:
            if generation == _cache_generation:
                _cache[key] = (monotonic() + _CACHE_SECONDS, rows)
                _cache.move_to_end(key)
                while len(_cache) > _CACHE_ENTRIES:
                    _cache.popitem(last=False)
        future.set_result(rows)
        return [dict(row) for row in rows]
    except BaseException as exc:
        future.set_exception(exc)
        raise
    finally:
        with _cache_lock:
            _inflight.pop(key, None)


def _calculate_daily_values(start, end):
    """Return observed energy, fully priced money and standing charges per local day."""
    end = min(end, datetime.now(timezone.utc))
    if start >= end:
        return []
    with get_engine().connect() as conn:
        settings = conn.execute(text("SELECT earnings_config,system_start_at FROM dashboard_settings WHERE dashboard_id=:dashboard"), {"dashboard":current_dashboard()}).mappings().first()
        config = settings.earnings_config if settings else {}
        system_start = settings.system_start_at.astimezone(LONDON).date() if settings and settings.system_start_at else None
        energy = conn.execute(text("""SELECT ds.code,ei.metric_code,ei.interval_start,ei.interval_end,ei.value_kwh
          FROM energy_intervals ei JOIN data_sources ds ON ds.id=ei.source_id
          WHERE ds.dashboard_id=:dashboard AND ei.interval_start < :end AND ei.interval_end > :start
          AND ((ds.code='octopus_import' AND ei.metric_code='grid_import_energy' AND ei.quality='settled')
            OR (ds.code='octopus_export' AND ei.metric_code='grid_export_energy' AND ei.quality='settled')
            OR (ds.code='octopus_gas' AND ei.metric_code='gas_consumption_kwh' AND ei.quality='settled')
            OR (ds.code='ecoflow_powerocean' AND ei.metric_code IN
                ('ecoflow_to_home','grid_export_energy','solar_generation_energy','battery_charge_energy','battery_discharge_energy','grid_import_energy')))
          ORDER BY ei.interval_start,ei.interval_end"""),
          {"dashboard":current_dashboard(),"start":start,"end":end}).mappings().all()
        tariffs = conn.execute(text("""SELECT fuel,direction,rate_kind,valid_from,valid_to,value_inc_vat_pence
          FROM tariff_rates WHERE dashboard_id=:dashboard AND origin='octopus_api'
          AND valid_from < :end AND (valid_to IS NULL OR valid_to > :start)
          ORDER BY valid_from,updated_at,id"""), {"dashboard":current_dashboard(),"start":midnight(start.astimezone(LONDON).date()),"end":end}).mappings().all()
    energy = [dict(row) for row in energy]
    curve_start = midnight(start.astimezone(LONDON).date())
    grouped = defaultdict(list)
    for tr in tariffs:
        grouped[(tr.fuel,tr.direction,tr.rate_kind)].append((tr.valid_from,tr.valid_to,tr.value_inc_vat_pence))
    curves = {key:Rates(rows,curve_start,end) for key,rows in grouped.items()}
    empty = Rates([],curve_start,end)
    eco_curves = {}
    for direction in ("import", "export"):
        entries = []
        for plan in config.get(direction+"_rates", []):
            if not plan.get("start_date") or not plan.get("end_date"):
                continue
            a = midnight(date.fromisoformat(plan["start_date"]))
            b = midnight(date.fromisoformat(plan["end_date"]) + timedelta(days=1))
            if plan.get("kind") == "flat":
                entries.append((a,b,Decimal(str(plan["pence"]))))
            elif plan.get("kind") == "dynamic":
                for x,y,value in grouped[("electricity",direction,"unit_rate")]:
                    if max(a,x) < min(b,y or end):
                        entries.append((max(a,x),min(b,y or end),value))
        eco_curves[direction] = Rates(entries,curve_start,end)

    # Prefer hourly data independently per metric/day; never add daily totals
    # to hourly readings for the same day. Today's cumulative remainder is
    # retained only over the unobserved time after the last hourly interval.
    buckets = defaultdict(list)
    local_dates = {}
    def local_day(moment):
        if moment not in local_dates:
            local_dates[moment] = moment.astimezone(LONDON).date()
        return local_dates[moment]
    for row in energy:
        if (row["code"],row["metric_code"]) in METRICS:
            buckets[(row["code"],row["metric_code"],local_day(row["interval_start"]))].append(row)
    selected = []
    today = datetime.now(timezone.utc).astimezone(LONDON).date()
    for (source,metric,day), rows in buckets.items():
        if source != "ecoflow_powerocean":
            selected.extend(rows)
            continue
        hourly = [r for r in rows if r["interval_end"]-r["interval_start"] <= timedelta(hours=1)]
        daily = [r for r in rows if r["interval_end"]-r["interval_start"] > timedelta(hours=1)]
        selected.extend(hourly or daily)
        if hourly and daily and day == today:
            last = max(r["interval_end"] for r in hourly)
            remainder = max(ZERO, max(r["value_kwh"] for r in daily)-sum((r["value_kwh"] for r in hourly),ZERO))
            if last < end and remainder > 0:
                selected.append(dict(code=source,metric_code=metric,interval_start=last,interval_end=end,value_kwh=remainder))
    values = defaultdict(lambda: defaultdict(list))
    day_ends = {}
    for row in selected:
        source, metric = row["code"],row["metric_code"]
        key,fuel,direction = METRICS[(source,metric)]
        a,b = max(start,row["interval_start"]),min(end,row["interval_end"])
        original_end = row["interval_end"]
        # An in-progress daily summary already represents only elapsed energy.
        if source == "ecoflow_powerocean" and local_day(row["interval_start"]) == today:
            original_end = min(original_end,end)
        duration = Decimal(str((original_end-row["interval_start"]).total_seconds()))
        while a < b:
            day = local_day(a)
            if day not in day_ends:
                day_ends[day] = midnight(day+timedelta(days=1))
            until = min(b,day_ends[day])
            kwh = row["value_kwh"] if a == row["interval_start"] and until == original_end else row["value_kwh"] * Decimal(str((until-a).total_seconds())) / duration
            values[day][key+"_kwh"].append(kwh)
            values[day][key+"_actual"].append(curves.get((fuel,direction,"unit_rate"),empty).price(a,until,kwh))
            if source == "ecoflow_powerocean":
                values[day][key+"_configured"].append(eco_curves[direction].price(a,until,kwh))
            a = until
    result = []
    day = start.astimezone(LONDON).date()
    while midnight(day) < end:
        row = {"date":day.isoformat(), "ecoflow_active":system_start is None or day >= system_start}
        for key in ("electricity","electricity_export","gas","ecoflow","ecoflow_export","ecoflow_battery","ecoflow_generation","ecoflow_discharge","ecoflow_import"):
            row[key+"_kwh"] = available_sum(values[day][key+"_kwh"])
            row[key+"_actual"] = available_sum(values[day][key+"_actual"])
            if key.startswith("ecoflow"):
                row[key+"_configured"] = available_sum(values[day][key+"_configured"])
        for key,fuel,direction in (("electricity","electricity","import"),("electricity_export","electricity","export"),("gas","gas","import")):
            rate = curves.get((fuel,direction,"standing_charge"),empty).at(midnight(day))
            row[key+"_standing"] = rate/HUNDRED if rate is not None else None
        # EcoFlow earnings follow the dashboard's configured rates. A dynamic
        # configured period uses the matching Octopus half-hour price, while a
        # flat configured period uses that saved rate throughout the interval.
        row["earnings_gbp"] = available_sum((
            row["ecoflow_configured"], row["ecoflow_export_configured"],
        ))
        row["octopus_earnings_gbp"] = available_sum((row["electricity_actual"],row["electricity_export_actual"]))
        row["calendar_earnings_gbp"] = row["earnings_gbp"]
        row["calendar_octopus_earnings_gbp"] = available_sum((
            row["ecoflow_actual"], row["electricity_export_actual"],
        )) if row["electricity_export_actual"] is not None else None
        row.update(net_values(row))
        result.append(row)
        day += timedelta(days=1)
    return result


def net_values(row):
    # A wholly unsettled day stays unknown; gaps do not erase observed amounts.
    imported, exported = row["electricity_actual"], row["electricity_export_actual"]
    electricity = None if (imported is None and exported is None) or row["electricity_standing"] is None else (exported or ZERO) - (imported or ZERO) - (row["electricity_standing"] or ZERO)
    gas = row["gas_actual"]
    return {"electricity_only_gbp":electricity,
        "including_gas_gbp":electricity-gas-(row["gas_standing"] or ZERO) if electricity is not None and gas is not None and row["gas_standing"] is not None else None}


def aggregate(rows, key):
    """Sum observed priced data; an entirely unpriced period stays unknown."""
    if key.startswith("ecoflow"):
        rows = [row for row in rows if row.get("ecoflow_active", True)]
    return available_sum(row[key] for row in rows)
