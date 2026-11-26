# Energy data schema

The database stores normalized readings for one household while keeping each provider or device source independently queryable. It stores no account identifiers, device serial numbers, credentials, or successful raw API/MQTT payloads.

## Sources and authority

| Source code | Purpose | Settlement authority |
| --- | --- | --- |
| `octopus_import` | Metered grid import used for billing | Yes |
| `octopus_export` | Metered grid export used for payment | Yes |
| `ecoflow_powerocean` | Inverter, battery and household measurements | No |
| `octopus_mini` | Low-latency smart-meter telemetry | No |

Authority is scoped to the matching grid import or export energy metric. EcoFlow and Mini values remain available for comparison and live status, but do not replace Octopus settlement readings.

## Canonical metrics

| Code | Kind | Unit |
| --- | --- | --- |
| `grid_import_energy` | energy | kWh |
| `grid_export_energy` | energy | kWh |
| `solar_generation_energy` | energy | kWh |
| `home_consumption_energy` | energy | kWh |
| `battery_charge_energy` | energy | kWh |
| `battery_discharge_energy` | energy | kWh |
| `solar_power` | power | W |
| `home_power` | power | W |
| `grid_power` | power | W |
| `battery_power` | power | W |
| `battery_soc` | ratio | % |

Adapters may add stable metrics for other successfully decoded PowerOcean fields. Each catalog entry declares whether its value is numeric, text, or boolean. Provider field names stay in adapter code.

## Historical intervals and tariffs

`energy_intervals` uses half-open `[interval_start, interval_end)` ranges. Values are non-negative directional kWh amounts; import/export and charge/discharge are separate metrics. An explicit zero means the provider reported zero. A missing reading has no row.

Quality means:

- `settled`: the provider's billing or settlement measurement.
- `reported`: a direct device/provider measurement that is not settlement data.
- `derived`: calculated from other measurements.
- `estimated`: an estimate explicitly identified as such.

Completeness is `complete` when the stored value represents the full interval and `partial` when it does not. It does not infer data before or after that interval.

`tariff_rates` stores import/export unit rates and standing charges in pence, both including and excluding VAT. Rates may be negative or zero. Costs are joined to authoritative readings and calculated when queried; calculated costs are not persisted.

All timestamps are timezone-aware and stored as PostgreSQL `timestamptz`. Storage and interval boundaries use UTC. Europe/London calendar days, including 23-hour and 25-hour DST days, are formed only in queries and presentation.

## Live telemetry

`telemetry_samples` is range-partitioned by UTC day. A sample contains exactly one of `value_number`, `value_text`, or `value_boolean`. Numeric changes are retained at every received sample; adapters may suppress consecutive identical categorical states. `telemetry_latest` is updated atomically with accepted historical inserts.

Raw telemetry is retained for 30 days. The collector creates partitions seven days ahead and builds idempotent one-minute and one-hour rollups for closed buckets. Numeric rollups contain minimum, maximum, average, last and count. Other types retain the last value and a change count. A PostgreSQL advisory lock prevents concurrent maintenance runs.

Before dropping an expired daily partition, maintenance rebuilds both resolutions and verifies that every raw source/metric bucket has both minute and hour coverage. Rollups are retained indefinitely. Capturing every PowerOcean field at full update frequency can consume substantial storage. `TelemetryMaintenance.storage_stats()` exposes raw row count, oldest raw sample, and rollup row count for later monitoring.

## Ingestion state

`ingestion_runs` records requests and sanitized outcomes. `coverage_windows` records whether requested half-open ranges were complete, partial, or failed. `sync_checkpoints` holds the latest successful cursor/watermark as an optimization hint. A checkpoint is not evidence that older data exists; coverage windows and actual interval rows determine completeness.

Repository inputs reject naive timestamps, empty/reversed ranges, negative energy, unknown source/metric codes, invalid typed telemetry, and credential/raw-payload markers in diagnostic fields.

