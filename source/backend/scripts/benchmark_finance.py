"""Read-only local benchmark: python -m scripts.benchmark_finance --as-of 2026-10-01.

Measures application-cold and application-warm calls against the same database.
The database buffer cache is not flushed. No emails or provider API calls occur.
"""
import argparse
from datetime import date
from statistics import median
from time import perf_counter

from sqlalchemy import event, text
from app import dashboard, finance, tenant, main as api
from app.database import get_engine


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date.today())
    parser.add_argument("--dashboard-id")
    parser.add_argument("--endpoints",action="store_true",help="Measure complete endpoint handlers (excluding HTTP/auth transport)")
    parser.add_argument("--iterations", type=int, default=3)
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("iterations must be positive")
    engine = get_engine()
    with engine.connect() as conn:
        did = args.dashboard_id or conn.execute(text("SELECT dashboard_id FROM dashboard_settings WHERE setup_complete=true ORDER BY system_start_at LIMIT 1")).scalar_one()
    scope = tenant.set_dashboard(str(did))
    count = [0]
    def tally(*_):
        count[0] += 1
    event.listen(engine, "before_cursor_execute", tally)
    try:
        month = dashboard.fiscal_start_month()
        year = args.as_of.year - (args.as_of.month < month)
        start, end = date(year,month,1), date(year+1,month,1)
        from datetime import timedelta
        work = {
            "overview_periods":lambda:dashboard.ecoflow_solar_periods(args.as_of),
            "projection":lambda:dashboard.annual_projection(args.as_of),
            "bills_year":lambda:dashboard.energy_bills_year(args.as_of.year),
            "earnings_year":lambda:dashboard.daily_earnings(start,end-timedelta(days=1)),
            "net_position":lambda:dashboard.net_position(args.as_of),
        }
        if args.endpoints:
            api.database_ready()
            work = {
                "overview_today":lambda:api.dashboard_overview_details("today",args.as_of),
                "overview_projection":lambda:api.dashboard_overview_details("projection",args.as_of),
                "overview_net":lambda:api.dashboard_overview_details("net-position",args.as_of),
                "bills":lambda:api.dashboard_energy_bills(args.as_of.year),
                "earnings":lambda:api.dashboard_daily_earnings(start,end-timedelta(days=1)),
                "net_calendar":lambda:api.dashboard_daily_net_position(start,end-timedelta(days=1),args.as_of),
                "imports":api.dashboard_operations,
            }
        for name, run in work.items():
            timings = {"cold":[], "warm":[]}
            queries = {"cold":[], "warm":[]}
            for _ in range(args.iterations):
                finance.invalidate_cache()
                for mode in timings:
                    count[0] = 0
                    began = perf_counter()
                    run()
                    timings[mode].append((perf_counter()-began)*1000)
                    queries[mode].append(count[0])
            print(name, {mode:{"median_ms":round(median(times),1),"sql_count":queries[mode]} for mode,times in timings.items()}, flush=True)
    finally:
        event.remove(engine, "before_cursor_execute", tally)
        tenant.reset_dashboard(scope)


if __name__ == "__main__":
    main()
