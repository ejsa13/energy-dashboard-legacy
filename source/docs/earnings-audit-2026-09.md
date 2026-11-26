# EcoFlow earnings audit — September 2026

This audit compares the EcoFlow mobile app's **Aggregated savings** calendar
with the dashboard for 1–28 September 2026. The two totals are calculated over
the same dates.

| Source | Total |
| --- | ---: |
| EcoFlow mobile app | £95.15 |
| Dashboard calculation | £105.09 |
| Difference | +£9.94 |

The dashboard's calculation is not using a reported EcoFlow savings value. No
`Aggregated savings`, `Total earnings`, or `Total savings` GBP metric has been
received from the configured history API, so every September row is marked as
calculated rather than reported.

## Trace

The screenshots for 28 September identify the incompatible energy meanings.

| Reading | Dashboard | EcoFlow Solar statistics |
| --- | ---: | ---: |
| From solar | 18.210 kWh | 18.21 kWh |
| To battery | 5.900 kWh | 5.86 kWh |
| To grid | 7.590 kWh | 7.48 kWh |
| To home | 8.360 kWh | 4.87 kWh |
| From battery | 3.490 kWh | not included in Solar `To home` |

The dashboard `To home` value is approximately EcoFlow Solar `To home` plus
`From battery`: `4.87 + 3.49 = 8.36`. It therefore represents the total energy
delivered to the house, rather than solar consumed directly by the house.

For the completed days from 1–28 September, repricing only direct solar use
(`solar generation − battery charge − grid export`) produces £66.86. The
current dashboard method, which values all self-supplied home energy, produces
£105.09. EcoFlow reports £95.15, so it credits £28.29 beyond direct solar use,
while the dashboard credits £38.23 beyond direct solar use.

That extra £28.29 varies by day. It cannot be represented by a fixed battery
efficiency or a fixed percentage of discharge. For example:

| Date | Direct-solar-only model | Dashboard model | EcoFlow app |
| --- | ---: | ---: | ---: |
| 1 Sep | £3.43 | £5.88 | £4.48 |
| 2 Sep | £1.13 | £2.67 | £3.14 |
| 12 Sep | £0.61 | £2.35 | £0.90 |
| 18 Sep | £1.71 | £3.06 | £2.77 |

On 2 September the app is even higher than the dashboard's all-battery model;
on 12 September it is far lower. This rules out a simple battery inclusion
toggle. The missing input is each battery discharge interval's source of
charge, together with the app's own interval-level treatment of that energy.

The current-day figure is live and changes as EcoFlow records new readings, so
the 28 September values are useful for energy-flow semantics but are not a
settled monetary comparison.

## Result

The £9.94 September discrepancy and £15.95 lifetime discrepancy are not caused
by the calendar totals. Those totals correctly sum their daily rows. They arise
because the available API supplies directional energy but not EcoFlow's daily
reported savings or battery-source provenance.

To make the figures identical, the dashboard must ingest EcoFlow's daily
reported Aggregated savings value, or EcoFlow must expose per-interval battery
provenance. Until then, the displayed number is a calculated estimate and
should not be presented as an exact app match.
