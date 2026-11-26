# Home Energy Dashboard

A local, mobile-first development scaffold for an Octopus Energy + EcoFlow PowerOcean dashboard.

EcoFlow and Octopus Mini are not connected yet. The collector imports Octopus grid-import settlement history and import tariffs when it has the required collector-only configuration. Live measurements intentionally remain `—` / `Not connected`.

## Start in WSL

Prerequisites: Docker Engine with Compose and Buildx, and Python 3 for the configuration script. These are already installed in the development WSL distribution.

```bash
cd /home/ejsta/repo/energy_dashboard
python3 scripts/setup.py
docker compose up --build -d --wait
```

- Dashboard: <http://localhost:5173>
- API docs: <http://localhost:8000/docs>
- Readiness: <http://localhost:8000/api/health/ready>

The setup script exclusively creates `.env.development` with a random local database password and permissions `0600`. It preserves any existing file. It never reads or modifies the original `.env` containing provider configuration. Do not replace `.env.development` while retaining the database volume: PostgreSQL only applies its initial password when creating a new database.

Compose loads `.env.development` into the API and database. The collector alone also receives the existing ignored `.env`, followed by `.env.development` so local database configuration takes precedence. The frontend has no access to either environment file. Git and Docker build contexts exclude secrets.

Octopus import requires `OCTOPUS_API_KEY` and `OCTOPUS_ACCOUNT_NUMBER` in `.env`. It discovers the electricity import meter and gas meter from the authenticated account response without storing identifiers. Gas backfill selects the serial with the newest reported interval; a tie fails safely, so former-meter data is not mixed into current gas history. Gas readings are stored as kWh for this account and gas tariffs are stored separately from electricity tariffs. Initial backfills begin at the earliest account agreement and refresh the prior 14 days every 24 hours for delayed readings and corrections.

EcoFlow history uses the collector-only Developer API access key, secret key, PowerOcean serial and `ECOFLOW_START_DATE` in `.env`. Its first request begins at midnight Europe/London on the first day of that month; daily reconciliation refreshes the preceding 14 days. It uses the official signed `/iot-open/sign/device/quota/data` history endpoint and accepts only its strict decoded response contract, recording a failed status without retaining raw data when the provider changes it.

Financial calculations use date-effective tariffs in pence, converted to pounds once. The overview's month/year/lifetime EcoFlow earnings use configured rates; its Octopus earnings sum settled import cost and export credit. Today's earnings and the Earnings calendar use EcoFlow home/export flows priced at Octopus rates. The calendar's Octopus comparison replaces EcoFlow export with settled Octopus export. Projection solar savings use home value minus battery-charge value plus battery-discharge value; battery savings use discharge value, and export credit uses settled Octopus export.

Period totals retain available priced readings instead of becoming unknown when one day is missing. Calendar settlement totals remain unknown when a period has no settled data. Overview daily net position uses EcoFlow import/export and the electricity standing charge; daily gas is unavailable. Calendar net positions and longer-period totals use settled Octopus energy and standing charges. Hourly EcoFlow readings take precedence over daily totals to avoid counting energy twice. Intervals spanning tariff changes are allocated by duration.

Finance requests batch energy and tariff reads, reuse a daily ledger within each response, and cache it for 15 seconds per dashboard. Saving settings invalidates the cache. Reproduce local cold/warm endpoint-handler measurements (excluding HTTP/auth transport) with `docker compose exec api python -m scripts.benchmark_finance --as-of 2026-10-01 --endpoints`. The database buffer cache is not flushed.

Tariff backfill checks actual scoped rate history as well as checkpoints, so a recent checkpoint cannot hide missing earlier rates. Old partial consumption windows are retried daily; recent reconciliation and failed requests are retried on the normal collector schedule. Provider settlement delays and genuine historical gaps remain visible as partial coverage.

### PowerOcean live telemetry

The collector uses the collector-only EcoFlow app settings to open a listen-only Enhanced Mode WSS MQTT connection. It records safely decoded values as ten-second snapshots and exposes only the approved solar, home, grid, battery, SOC and system-status values to the dashboard. MQTT readings are marked stale after 35 seconds and the worker reconnects; this PowerOcean setup does not use the unsupported `/quota/all` Developer API fallback. Credentials, identifiers and MQTT topics never reach the API or browser. The collector writes a bounded, decoded and redacted payload summary to its log by default; set `COLLECTOR_LOG_RAW_PAYLOADS=false` to disable it. Secrets, device identifiers and tokens are always removed. The Enhanced Mode login and certificate-decryption flow is adapted from the MIT-licensed [EcoFlow Energy HA](https://github.com/shuette42/ecoflow-energy-ha) project.

This is a **localhost-only development environment without application authentication**. Ports 5173 and 8000 bind to loopback; the database has no published port. It is not configured for phone access over the LAN or production deployment.

## Services and development

| Service | Responsibility |
| --- | --- |
| `frontend` | Node 24, React, TypeScript, Vite and Tailwind; ECharts installed for future history views |
| `api` | Python 3.13, FastAPI, SQLAlchemy and Psycopg; runs Alembic before starting Uvicorn |
| `collector` | Heartbeat, telemetry maintenance, and Octopus historical import/reconciliation |
| `db` | PostgreSQL 17 with a persistent named volume |

Source directories are bind-mounted. Frontend changes hot-reload; API changes trigger Uvicorn reload. Changes to collector source require a restart. Application processes run as non-root users. The frontend's `node_modules` lives in its own named volume; `npm ci` synchronises it to the committed lockfile on startup.

```bash
docker compose ps
docker compose logs -f api collector
docker compose restart collector
docker compose exec api alembic upgrade head
docker compose exec api alembic current
docker compose exec api pytest -q
docker compose exec frontend npm run typecheck
docker compose exec frontend npm run build
```

After modifying Python requirements, regenerate `backend/requirements.lock` with pip-tools in Python 3.13, then rebuild the backend. The lock includes hashes and development tests. After modifying frontend dependencies, update `package-lock.json` with Node 24 and rebuild. Runtime images use versioned tags; application dependencies are locked. Image digests should be pinned during production packaging.

```bash
# Stop containers while retaining database and node_modules volumes.
docker compose down
# Resume using the existing database.
docker compose up -d --wait
```

Do not add `--volumes` / `-v` to `down` unless you intend to erase the database and dependencies. Do not delete `.env.development` as part of routine cleanup.

## API and data semantics

- `GET /api/health/live`: returns 200 while the API process responds, independently of PostgreSQL.
- `GET /api/health/ready`: returns 200 only when PostgreSQL is reachable and the expected Alembic revision is installed; otherwise 503.
- `GET /api/status`: UTC observation time, database readiness, collector state/heartbeat/age, and provider state. Octopus reports `backfilling`, `syncing`, `healthy`, or `failed` from its latest ingestion run; EcoFlow and Mini remain `not_connected`.
- `GET /api/events`: SSE `status` events, immediately and then every five seconds. Each event has the same JSON shape as `/api/status`. Cache and buffering are disabled; disconnects cancel the stream task.

The frontend uses a Vite `/api` proxy, so HTTP and SSE share one browser origin. It refreshes the snapshot when SSE connects/reconnects and does not present old infrastructure state as healthy while the stream is interrupted. A 15-second silence watchdog reconnects even if the proxy leaves an interrupted connection open.

The collector writes a UTC timestamp every ten seconds. Its state is `waiting` before the first heartbeat, `healthy` through 30 seconds, `stale` after 30 seconds, or `unknown` when the database cannot be queried. Failed database writes retry after five seconds. Octopus import runs in a separate worker at collector startup and every 24 hours, so a long history backfill does not make the collector stale.

```bash
docker compose restart collector
docker compose logs -f collector
```

The normalized PostgreSQL schema covers historical intervals, tariffs, live telemetry, rollups and ingestion progress. Octopus import settlement readings are authoritative. Missing intervals have no row; zero remains a valid reading. See [docs/data-schema.md](docs/data-schema.md) for canonical metric codes, timestamp semantics, quality meanings and the 30-day raw telemetry retention policy. This milestone does not add history or telemetry HTTP endpoints.

## Acceptance checks

After the stack is running:

```bash
docker compose exec api pytest -q
docker compose exec frontend npm run build
python3 scripts/verify_stack.py
```

The verification script deliberately stops and restarts **this Compose project's** database, collector and API, and cycles the stack to verify persistence. Run it when temporary local downtime is acceptable. It checks readiness recovery, collector freshness, SSE through Vite, repeatable migrations and volume persistence. It leaves the stack running. It does not wait for an Octopus import to finish.

Inspect the dashboard at desktop and 375px/320px widths. Confirm keyboard focus, navigation links, no horizontal overflow and no invented readings. To observe the browser reconnect state, run `docker compose stop api`, wait for “Status stream interrupted”, then `docker compose start api`.

## Next milestone

1. Adapt the Enhanced Mode connection, keepalives and PowerOcean parsers from `/home/ejsta/repo/ecoflow-energy-ha`; retain its MIT attribution for reused code and map every decoded field into typed telemetry metrics.
2. Add Octopus export settlement readings.
4. Add Octopus Mini `smartMeterTelemetry` using Octopus GraphQL authentication and store its readings under `octopus_mini`.
5. Add history and live dashboard APIs backed by the repository layer, with Octopus settlement data labelled as authoritative.

Live energy, backfills, installation on a phone, authentication and Proxmox deployment are explicitly outside this scaffold.

### Octopus Mini live telemetry

The collector uses the existing Octopus API key and account number to discover one electricity Mini device from the account. It polls the Octopus GraphQL telemetry service every 10 seconds using a two-minute overlap, so short cloud delays are recovered by idempotent upserts. Raw Mini samples are retained for 30 days and rollups are retained indefinitely. Mini values and costs are live provider telemetry, not settlement or billing records; half-hourly Octopus readings remain authoritative. Restart it after configuration changes with .

## Production accounts

Production uses invite-only Google sign-in. Create the production secret file outside the repository at /opt/energy-dashboard/secrets/.env.production from .env.production.example, keep it mode 0600, and set PUBLIC_APP_URL to the HTTPS Tailscale address registered in Google Cloud OAuth. Add https://your-host.tailnet.ts.net/api/auth/google/callback to that OAuth client.

Start the production stack with DASHBOARD_ENV_FILE=/opt/energy-dashboard/secrets/.env.production docker compose --env-file /opt/energy-dashboard/secrets/.env.production -f production-compose.yaml up -d.

The address in INITIAL_ADMIN_EMAIL becomes the first administrator on its first Google login. Administrators create invitations through POST /api/admin/invitations; invited users complete their own dashboard setup. Provider credentials and projections are dashboard-scoped and credentials are encrypted using DASHBOARD_CREDENTIALS_KEY.