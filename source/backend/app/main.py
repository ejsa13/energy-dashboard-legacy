import asyncio
import csv
import json
import re
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace

from fastapi import FastAPI, Request, Depends
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import JSONResponse, StreamingResponse, RedirectResponse, Response
from sqlalchemy import text

from app.database import get_engine
from app.status import SystemStatus, database_ready, read_status
from app.repositories import LiveProviderHealthRepository, TelemetryRepository
from app.repositories import CatalogRepository
from app import dashboard
from app import finance
from app import auth
from app import tenant
from app.emailer import send_invitation
from app.octopus import OctopusApiError, OctopusClient, parse_api_datetime
from app.axle import fetch_event
from datetime import date, datetime, time, timezone, timedelta
from decimal import Decimal
from fastapi import HTTPException


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    get_engine().dispose()


app = FastAPI(title="Energy Dashboard", version="0.1.0", lifespan=lifespan)
collector_logs: dict[str, deque[str]] = defaultdict(lambda: deque(maxlen=500))

class SessionRequiredMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        public = path.startswith("/api/auth/") or path.startswith("/api/health/") or path in {"/api/status", "/api/internal/collector-log", "/docs", "/openapi.json"}
        if path.startswith("/api/") and not public:
            try:
                identity = auth.current_identity(request)
            except HTTPException as exc: return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            if identity.get("selection_required"):
                return JSONResponse({"detail":"Select a dashboard first"}, status_code=409)
            scope = tenant.set_dashboard(str(identity["dashboard_id"]))
            try:
                return await call_next(request)
            finally:
                tenant.reset_dashboard(scope)
        return await call_next(request)

app.add_middleware(SessionRequiredMiddleware)

@app.get("/api/auth/me")
def auth_me(identity: dict = Depends(auth.current_identity)):
    with get_engine().connect() as conn:
        has_projections = bool(conn.execute(text("SELECT EXISTS(SELECT 1 FROM projection_targets WHERE dashboard_id=:id)"), {"id": identity["dashboard_id"]}).scalar())
    return {"dashboards": identity.get("dashboards", []), "selection_required": identity.get("selection_required", False), "user": {"email": identity["email"], "name": identity["display_name"], "admin": auth.is_initial_admin(identity)}, "dashboard": {"is_owner": identity["is_owner"], "id": str(identity["dashboard_id"]), "setup_complete": identity["setup_complete"], "system_start_at": identity["system_start_at"].isoformat() if identity["system_start_at"] else None, "has_projections": has_projections}}

@app.post("/api/auth/dashboard")
async def select_dashboard(request: Request, identity: dict = Depends(auth.current_identity)):
    auth.require_csrf(request, identity)
    payload = await request.json()
    dashboard_id = str(payload.get("dashboard_id") or "")
    with get_engine().begin() as conn:
        updated = conn.execute(text("""UPDATE user_sessions SET selected_dashboard_id=m.dashboard_id
          FROM dashboard_memberships m WHERE user_sessions.token_hash=:hash
          AND m.user_id=user_sessions.user_id AND m.dashboard_id::text=:dashboard RETURNING m.dashboard_id"""),
          {"hash":auth._hash(request.cookies.get(auth.SESSION_COOKIE, "")),"dashboard":dashboard_id}).scalar()
        if updated is None:
            raise HTTPException(403, "You do not have access to this dashboard")
    return {"selected":True}

@app.get("/api/auth/google")
def auth_google():
    state = auth.make_oauth_state()
    response = RedirectResponse(auth.google_authorize_url(state))
    response.set_cookie(auth.STATE_COOKIE, state, httponly=True, secure=True, samesite="lax", max_age=600)
    return response

@app.get("/api/auth/google/callback")
def auth_google_callback(request: Request, code: str, state: str):
    if not auth.valid_oauth_state(state) or not __import__('secrets').compare_digest(state, request.cookies.get(auth.STATE_COOKIE, "")):
        return RedirectResponse("/?sign_in=expired")
    try:
        token, csrf = auth.create_or_login(auth.google_identity(code))
    except HTTPException as exc:
        # OAuth callbacks are browser navigation, so return people to the
        # sign-in screen with an explanation instead of exposing API JSON.
        reason = "not-invited" if exc.status_code == 403 else "failed"
        response = RedirectResponse(f"/?sign_in={reason}")
        response.delete_cookie(auth.STATE_COOKIE)
        return response
    response = RedirectResponse("/")
    response.set_cookie(auth.SESSION_COOKIE, token, httponly=True, secure=True, samesite="lax", max_age=14 * 24 * 3600)
    response.set_cookie("energy_csrf", csrf, httponly=False, secure=True, samesite="lax", max_age=14 * 24 * 3600)
    response.delete_cookie(auth.STATE_COOKIE)
    return response

@app.post("/api/auth/logout")
def auth_logout(request: Request, identity: dict = Depends(auth.current_identity)):
    auth.require_csrf(request, identity)
    with get_engine().begin() as conn: conn.execute(text("DELETE FROM user_sessions WHERE token_hash=:hash"), {"hash":auth._hash(request.cookies.get(auth.SESSION_COOKIE, ""))})
    response = Response(status_code=204); response.delete_cookie(auth.SESSION_COOKIE); response.delete_cookie("energy_csrf"); return response

@app.post("/api/admin/invitations")
async def create_invitation(request: Request, identity: dict = Depends(auth.current_identity)):
    auth.require_csrf(request, identity)
    if not auth.is_initial_admin(identity):
        raise HTTPException(403, "Only the configured initial administrator can create invitations")
    payload = await request.json()
    email = str(payload.get("email") or "").strip().lower()
    invitation_type = str(payload.get("type") or "dashboard_access")
    if len(email) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise HTTPException(422, "A valid email address is required")
    if invitation_type not in {"dashboard_access", "new_dashboard"}:
        raise HTTPException(422, "Invitation type must be dashboard_access or new_dashboard")
    token = __import__("secrets").token_urlsafe(32)
    dashboard_url = __import__('os').environ.get('PUBLIC_APP_URL', '').rstrip('/')
    with get_engine().begin() as conn:
        # Serialize invitations for the same address, including the initial insert.
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:email))"), {"email":email})
        dashboard_id = identity["dashboard_id"] if invitation_type == "dashboard_access" else None
        existing = conn.execute(text("SELECT * FROM invitations WHERE email=:email AND dashboard_id IS NOT DISTINCT FROM CAST(:dashboard AS uuid) FOR UPDATE"), {"email":email,"dashboard":dashboard_id}).mappings().first()
        if existing:
            if str(existing.invited_by) != str(identity["user_id"]):
                raise HTTPException(403, "This invitation belongs to another administrator")
            if existing.consumed_at:
                raise HTTPException(409, "This invitation has already been accepted")
            if existing.expires_at > datetime.now(timezone.utc):
                raise HTTPException(409, "Reinvite is available after seven days without acceptance")
            invitation_type = "dashboard_access" if existing.dashboard_id else "new_dashboard"
        dashboard_id = existing.dashboard_id if existing else identity["dashboard_id"] if invitation_type == "dashboard_access" else None
        dashboard_name = conn.execute(text("SELECT display_name FROM dashboard_accounts WHERE id=:id"), {"id":dashboard_id or identity["dashboard_id"]}).scalar_one()
        conn.execute(text("""INSERT INTO invitations(email,token_hash,invited_by,dashboard_id,expires_at,email_status)
          VALUES (:email,:token,:user,:dashboard,now() + interval '7 days','Sent')
          ON CONFLICT ON CONSTRAINT invitations_email_dashboard_key DO UPDATE SET token_hash=excluded.token_hash,
          expires_at=excluded.expires_at,created_at=now(),email_status='Sent'
          """), {"email":email,"token":auth._hash(token),"user":identity["user_id"],"dashboard":dashboard_id})
        # Roll back on SMTP failure so a failed send does not start the cooldown.
        try:
            send_invitation(recipient=email, dashboard_name=str(dashboard_name) if invitation_type == "dashboard_access" else None, dashboard_url=dashboard_url)
        except RuntimeError:
            raise HTTPException(503, "Invitation email could not be sent. Please check email configuration and try again.")
        except Exception:
            raise HTTPException(502, "Invitation email could not be sent. Please try again.")
    return {"email":email,"expires_in_days":7,"sent":True}

@app.get("/api/admin/invitations")
def list_invitations(identity: dict = Depends(auth.current_identity), invitation_type: str | None = None):
    if not auth.is_initial_admin(identity):
        raise HTTPException(403, "Only the configured initial administrator can view invitations")
    with get_engine().connect() as conn:
        rows = conn.execute(text("""SELECT email,dashboard_id,created_at,expires_at,consumed_at,email_status
          FROM invitations WHERE invited_by=:user
          ORDER BY created_at DESC"""), {"user":identity["user_id"]}).mappings().all()
    if invitation_type == "new_dashboard":
        rows = [row for row in rows if row.dashboard_id is None]
    elif invitation_type == "dashboard_access":
        rows = [row for row in rows if str(row.dashboard_id) == str(identity["dashboard_id"])]
    elif invitation_type is not None:
        raise HTTPException(422, "Invalid invitation type")
    now = datetime.now(timezone.utc)
    return {"invitations":[{"email":row.email,"type":"dashboard_access" if row.dashboard_id else "new_dashboard","created_at":row.created_at.isoformat(),"expires_at":row.expires_at.isoformat(),"email_status":row.email_status,"status":"Accepted" if row.consumed_at else "Expired" if row.expires_at <= now else "Pending","can_reinvite":not row.consumed_at and row.expires_at <= now} for row in rows]}

@app.delete("/api/admin/invitations")
async def delete_invitation(request: Request, identity: dict = Depends(auth.current_identity)):
    auth.require_csrf(request, identity)
    if not auth.is_initial_admin(identity):
        raise HTTPException(403, "Only the configured initial administrator can delete invitations")
    payload = await request.json()
    email = str(payload.get("email") or "").strip().lower()
    invitation_type = payload.get("type", "dashboard_access")
    if invitation_type not in {"dashboard_access", "new_dashboard"}:
        raise HTTPException(422, "Invalid invitation type")
    with get_engine().begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:email))"), {"email":email})
        removed = conn.execute(text("DELETE FROM invitations WHERE email=:email AND invited_by=:user AND dashboard_id IS NOT DISTINCT FROM CAST(:dashboard AS uuid) RETURNING dashboard_id"), {"email":email,"user":identity["user_id"],"dashboard":identity["dashboard_id"] if invitation_type == "dashboard_access" else None}).mappings().first()
        if removed is None:
            raise HTTPException(404, "Invitation not found")
        stored_type = "dashboard_access" if removed.dashboard_id else "new_dashboard"
        if payload.get("type", stored_type) != stored_type:
            raise HTTPException(409, "Invitation type has changed. Refresh the invitation list.")
        if removed.dashboard_id:
            owner_email = conn.execute(text("SELECT u.email FROM dashboard_accounts d JOIN users u ON u.id=d.owner_user_id WHERE d.id=:dashboard"), {"dashboard":removed.dashboard_id}).scalar_one()
            if owner_email == email:
                raise HTTPException(403, "The dashboard owner's access cannot be removed")
            conn.execute(text("""DELETE FROM dashboard_memberships
              WHERE dashboard_id=:dashboard AND user_id IN (SELECT id FROM users WHERE email=:email)"""),
              {"dashboard":removed.dashboard_id,"email":email})
        else:
            target = conn.execute(text("""SELECT d.id,u.id AS owner_id,u.is_admin FROM dashboard_accounts d
              JOIN users u ON u.id=d.owner_user_id WHERE u.email=:email FOR UPDATE OF d"""), {"email":email}).mappings().first()
            if target:
                if target.is_admin or str(target.owner_id) == str(identity["user_id"]) or str(target.id) == str(identity["dashboard_id"]):
                    raise HTTPException(403, "Cannot delete the administrator's dashboard")
                # Use the target tenant for RLS-protected source and tariff rows.
                conn.execute(text("SELECT set_config('app.dashboard_id', :dashboard, true)"), {"dashboard":str(target.id)})
                conn.execute(text("SELECT id FROM data_sources WHERE dashboard_id=:dashboard FOR UPDATE"), {"dashboard":target.id}).all()
                # These source children use RESTRICT; all remaining children cascade.
                for table in ("energy_intervals", "telemetry_samples", "ingestion_runs"):
                    conn.execute(text(f"DELETE FROM {table} WHERE source_id IN (SELECT id FROM data_sources WHERE dashboard_id=:dashboard)"), {"dashboard":target.id})
                conn.execute(text("DELETE FROM dashboard_accounts WHERE id=:dashboard"), {"dashboard":target.id})
    return Response(status_code=204)

@app.get("/api/setup")
def setup_read(identity: dict = Depends(auth.current_identity)):
    if not identity.get("is_owner", False):
        raise HTTPException(403, "Only the dashboard owner can access dashboard settings")
    with get_engine().connect() as conn:
        settings = conn.execute(text("SELECT display_name,timezone,system_start_at,earnings_config,setup_complete FROM dashboard_accounts d JOIN dashboard_settings s ON s.dashboard_id=d.id WHERE d.id=:id"), {"id":identity["dashboard_id"]}).mappings().one()
        projections = conn.execute(text("SELECT fiscal_year,generation_kwh,solar_gbp,battery_gbp,export_gbp,cost_gbp,net_position_gbp FROM projection_targets WHERE dashboard_id=:id ORDER BY fiscal_year"), {"id":identity["dashboard_id"]}).mappings().all()
        providers = conn.execute(text("SELECT provider,ciphertext,nonce,updated_at FROM provider_credentials WHERE dashboard_id=:id"), {"id":identity["dashboard_id"]}).mappings().all()
    credentials = {row.provider: auth.decrypt_credentials(str(identity["dashboard_id"]), row.provider, bytes(row.ciphertext), bytes(row.nonce)) for row in providers}
    return {"settings":dict(settings),"projections":[dict(row) for row in projections],"providers":[{"provider":row.provider,"updated_at":row.updated_at} for row in providers],"credentials":credentials}


@app.get("/api/dashboard/vpp")
def vpp_dashboard(identity: dict = Depends(auth.current_identity)):
    """Fetch and record Axle's current/next event, then return its timeline."""
    dashboard_id = str(identity["dashboard_id"])
    with get_engine().connect() as connection:
        row = connection.execute(text("SELECT ciphertext,nonce FROM provider_credentials WHERE dashboard_id=:id AND provider='axle_vpp'"), {"id": dashboard_id}).mappings().first()
    if row is None:
        return {"configured": False, "event": None, "events": []}
    credentials = auth.decrypt_credentials(dashboard_id, "axle_vpp", bytes(row.ciphertext), bytes(row.nonce))
    api_key = str(credentials.get("api_key") or "").strip()
    if not api_key:
        return {"configured": False, "event": None, "events": []}
    event = fetch_event(api_key)
    if event is not None:
        with get_engine().begin() as connection:
            connection.execute(text("""
                INSERT INTO vpp_events(dashboard_id,provider,start_at,end_at,direction,provider_updated_at,last_seen_at)
                VALUES (:dashboard,'axle',:start,:end,:direction,:updated,now())
                ON CONFLICT(dashboard_id,provider,start_at,end_at) DO UPDATE
                SET direction=excluded.direction,provider_updated_at=excluded.provider_updated_at,last_seen_at=now()
            """), {"dashboard": dashboard_id, "start": event["start_at"], "end": event["end_at"], "direction": event["direction"], "updated": event["updated_at"]})
    with get_engine().connect() as connection:
        rows = connection.execute(text("""
            SELECT event.start_at,event.end_at,event.direction,event.provider_updated_at,
              COALESCE((SELECT SUM(intervals.value_kwh) FROM energy_intervals intervals
                JOIN data_sources source ON source.id=intervals.source_id
                WHERE source.dashboard_id=event.dashboard_id AND source.code='octopus_export'
                  AND intervals.metric_code='grid_export_energy' AND intervals.quality='settled'
                  AND intervals.interval_start >= event.start_at AND intervals.interval_end <= event.end_at), 0) AS settled_export_kwh
            FROM vpp_events event WHERE event.dashboard_id=:dashboard
            ORDER BY event.start_at DESC LIMIT 20
        """), {"dashboard": dashboard_id}).mappings().all()
    now = datetime.now(timezone.utc)
    result = [{"start_at": entry.start_at.isoformat(), "end_at": entry.end_at.isoformat(), "direction": entry.direction, "updated_at": entry.provider_updated_at.isoformat() if entry.provider_updated_at else None, "settled_export_kwh": str(entry.settled_export_kwh), "state": "in_progress" if entry.start_at <= now < entry.end_at else "upcoming" if now < entry.start_at else "finished"} for entry in rows]
    return {"configured": True, "event": next((entry for entry in result if entry["state"] in {"in_progress", "upcoming"}), None), "events": result}


@app.get("/api/exports/energy.csv")
def export_energy_csv(identity: dict = Depends(auth.current_identity)):
    """Export the source intervals needed for tariff and self-consumption analysis.

    EcoFlow retains its native hourly readings.  Octopus readings are restricted
    to settled electricity import/export intervals, which are normally half-hourly.
    A long/tidy format lets an analysis tool safely distinguish sources, metrics,
    resolutions and data quality without assuming intervals can be added together.
    """
    dashboard_id = str(identity["dashboard_id"])

    def csv_rows():
        output = StringIO()
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow((
            "interval_start_utc", "interval_end_utc", "resolution_minutes",
            "provider", "source", "metric", "value_kwh", "quality", "completeness",
        ))
        yield output.getvalue()
        output.seek(0)
        output.truncate(0)

        statement = text("""
            SELECT
              ei.interval_start,
              ei.interval_end,
              EXTRACT(EPOCH FROM (ei.interval_end - ei.interval_start)) / 60 AS resolution_minutes,
              ds.provider,
              ds.code AS source,
              ei.metric_code AS metric,
              ei.value_kwh,
              ei.quality,
              ei.completeness
            FROM energy_intervals ei
            JOIN data_sources ds ON ds.id = ei.source_id
            WHERE ds.dashboard_id = :dashboard_id
              AND (
                (ds.code = 'ecoflow_powerocean'
                  AND ei.interval_end - ei.interval_start = INTERVAL '1 hour')
                OR (ds.code = 'octopus_import'
                  AND ei.metric_code = 'grid_import_energy'
                  AND ei.quality = 'settled')
                OR (ds.code = 'octopus_export'
                  AND ei.metric_code = 'grid_export_energy'
                  AND ei.quality = 'settled')
              )
            ORDER BY ei.interval_start, ei.interval_end, ds.code, ei.metric_code
        """)
        with get_engine().connect() as connection:
            result = connection.execution_options(stream_results=True).execute(
                statement, {"dashboard_id": dashboard_id}
            ).mappings()
            for row in result:
                writer.writerow((
                    row["interval_start"].isoformat(),
                    row["interval_end"].isoformat(),
                    int(row["resolution_minutes"]),
                    row["provider"], row["source"], row["metric"],
                    str(row["value_kwh"]), row["quality"], row["completeness"],
                ))
                yield output.getvalue()
                output.seek(0)
                output.truncate(0)

    filename = f"home-energy-data-{date.today().isoformat()}.csv"
    return StreamingResponse(
        csv_rows(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _tariff_curve(client: OctopusClient, tariff_code: str, start: datetime, end: datetime, *, standing: bool = False) -> finance.Rates:
    rows = client.standing_charges(tariff_code=tariff_code, start=start, end=end) if standing else client.unit_rates(tariff_code=tariff_code, start=start, end=end)
    entries = []
    for row in rows:
        try:
            valid_from = parse_api_datetime(row["valid_from"])
            valid_to = parse_api_datetime(row["valid_to"]) if row.get("valid_to") else None
            value = Decimal(str(row["value_inc_vat"]))
        except (KeyError, TypeError, ValueError, ArithmeticError, OctopusApiError) as exc:
            raise HTTPException(502, "Octopus returned an invalid tariff rate") from exc
        entries.append((valid_from, valid_to, value))
    return finance.Rates(entries, start, end)


def _tariff_comparison_value(rows, curve: finance.Rates) -> Decimal | None:
    """Price each recorded kWh interval at the tariff rate active then.

    `Rates.price` allocates an interval across every rate boundary it spans;
    it never substitutes a period-average rate for the recorded profile.
    """
    prices = [curve.price(row.interval_start, row.interval_end, row.value_kwh) for row in rows]
    return sum(prices, Decimal("0")) if prices and all(price is not None for price in prices) else None


def _apply_evening_battery_dispatch(rows, capacity_kwh: Decimal, discharge_percent: Decimal, start_time: str, end_time: str):
    """Release one daily allowance uniformly over the selected local-time window.

    The selected percentage applies to the battery capacity once per day, not
    once per hour. Battery output serves recorded import first; any surplus
    becomes simulated grid export at the candidate tariff's active rate.
    """
    start_hour, start_minute = (int(value) for value in start_time.split(":"))
    end_hour, end_minute = (int(value) for value in end_time.split(":"))
    daily_allowance = capacity_kwh * discharge_percent / Decimal("100")
    dispatched = Decimal("0")
    adjusted_imports = []
    simulated_exports = []
    for row in rows:
        local_start = row.interval_start.astimezone(finance.LONDON)
        local_day = local_start.date()
        window_start = datetime.combine(local_day, time(start_hour, start_minute), finance.LONDON).astimezone(timezone.utc)
        window_end = datetime.combine(local_day, time(end_hour, end_minute), finance.LONDON).astimezone(timezone.utc)
        overlap_start, overlap_end = max(row.interval_start, window_start), min(row.interval_end, window_end)
        output = Decimal("0")
        if overlap_end > overlap_start:
            window_seconds = Decimal(str((window_end - window_start).total_seconds()))
            overlap_seconds = Decimal(str((overlap_end - overlap_start).total_seconds()))
            output = daily_allowance * overlap_seconds / window_seconds
            dispatched += output
        import_after_discharge = max(Decimal("0"), row.value_kwh - output)
        adjusted_imports.append(SimpleNamespace(interval_start=row.interval_start, interval_end=row.interval_end, value_kwh=import_after_discharge))
        if output > row.value_kwh:
            simulated_exports.append(SimpleNamespace(
                interval_start=row.interval_start,
                interval_end=row.interval_end,
                value_kwh=output - row.value_kwh,
            ))
    return adjusted_imports, simulated_exports, dispatched


def _actual_battery_capacity_kwh(dashboard_id: str) -> Decimal | None:
    """Return the current full capacity reported by the connected PowerOcean."""
    with get_engine().connect() as connection:
        value = connection.execute(text("""
            SELECT tl.value_number
            FROM telemetry_latest tl JOIN data_sources ds ON ds.id=tl.source_id
            WHERE ds.dashboard_id=:dashboard_id AND ds.code='ecoflow_powerocean'
              AND tl.metric_code='ecoflow_battery_capacity_wh' AND tl.value_number > 0
            ORDER BY tl.sampled_at DESC LIMIT 1
        """), {"dashboard_id": dashboard_id}).scalar()
    if value is None:
        return None
    capacity = Decimal(str(value)) / Decimal("1000")
    return capacity if capacity <= Decimal("1000") else None


@app.get("/api/dashboard/tariff-comparison/defaults")
def tariff_comparison_defaults(identity: dict = Depends(auth.current_identity)):
    agreements = dashboard.current_tariff_agreements()
    result = {
        "import_tariff_code": agreements["electricity_import"]["tariff_code"],
        "export_tariff_code": agreements["electricity_export"]["tariff_code"],
        "battery_capacity_kwh": finance.money(_actual_battery_capacity_kwh(str(identity["dashboard_id"]))),
        "import_options": [], "export_options": [],
    }
    current_import = result["import_tariff_code"]
    if not isinstance(current_import, str) or "-" not in current_import:
        return result
    region = current_import.rsplit("-", 1)[-1]
    config = auth.collector_configuration(str(identity["dashboard_id"]))
    if config is None:
        return result
    try:
        client = OctopusClient(config["OCTOPUS_API_KEY"])
        products = client.products()
        blocked = ("OUTGOING", "EXPORT", "SEG", "ZERO", "COOP", "LP-", "PREPAY", "SMB", "POWER-PACK", "SNUG", "COSY")
        imports = [item for item in products if isinstance(item.get("code"), str) and not any(term in item["code"].upper() for term in blocked)]
        exports = [item for item in products if isinstance(item.get("code"), str) and ("OUTGOING" in item["code"].upper() or "FLUX-EXPORT" in item["code"].upper())]
        def options(items):
            values = []
            for item in items:
                detail = client.product(str(item["code"]))
                tariff = (detail.get("single_register_electricity_tariffs") or {}).get("_" + region, {}).get("direct_debit_monthly") or {}
                code = tariff.get("code")
                if isinstance(code, str):
                    values.append({"code": code, "label": str(item.get("display_name") or item["code"])})
            return values
        result["import_options"] = options(imports)
        result["export_options"] = options(exports)
    except OctopusApiError:
        # The saved agreement remains usable even when the public catalogue is temporarily unavailable.
        pass
    return result


@app.post("/api/dashboard/tariff-comparison")
async def tariff_comparison(request: Request, identity: dict = Depends(auth.current_identity)):
    """Price recorded settled intervals against user-selected Octopus tariffs.

    This deliberately keeps the measured import/export profile fixed. It is a
    transparent historical tariff comparison, not a battery-dispatch forecast.
    """
    auth.require_csrf(request, identity)
    payload = await request.json()
    try:
        start_day = date.fromisoformat(str(payload["start"]))
        end_day = date.fromisoformat(str(payload["end"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(422, "Choose a valid comparison date range") from exc
    if end_day <= start_day or end_day - start_day > timedelta(days=93):
        raise HTTPException(422, "Choose a comparison period between 1 and 93 days")
    battery_dispatch = payload.get("battery_dispatch") or {}
    if not isinstance(battery_dispatch, dict):
        raise HTTPException(422, "Battery dispatch settings must be an object")
    enabled = battery_dispatch.get("enabled", False)
    if not isinstance(enabled, bool):
        raise HTTPException(422, "Battery dispatch must be enabled or disabled")
    try:
        discharge_percent = Decimal(str(battery_dispatch.get("discharge_percent", 50)))
    except (TypeError, ValueError, ArithmeticError) as exc:
        raise HTTPException(422, "Battery discharge must be a percentage") from exc
    discharge_start = str(battery_dispatch.get("start_time", "17:00"))
    discharge_end = str(battery_dispatch.get("end_time", "19:00"))
    if not Decimal("0") <= discharge_percent <= Decimal("100") or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", discharge_start) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", discharge_end) or discharge_start >= discharge_end:
        raise HTTPException(422, "Choose a battery discharge from 0–100% and valid times")
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 5 or not all(isinstance(candidate, dict) for candidate in candidates):
        raise HTTPException(422, "Add between one and five tariff combinations")
    start, end = finance.midnight(start_day), finance.midnight(end_day)
    dashboard_id = str(identity["dashboard_id"])
    capacity_kwh = _actual_battery_capacity_kwh(dashboard_id) if enabled else None
    if enabled and capacity_kwh is None:
        raise HTTPException(409, "EcoFlow has not reported battery capacity yet; wait for live telemetry before enabling battery dispatch")
    config = auth.collector_configuration(dashboard_id)
    if config is None or not config.get("OCTOPUS_API_KEY"):
        raise HTTPException(409, "Connect Octopus in Settings before comparing tariffs")
    with get_engine().connect() as connection:
        readings = connection.execute(text("""
            SELECT ds.code, ei.interval_start, ei.interval_end, ei.value_kwh
            FROM energy_intervals ei JOIN data_sources ds ON ds.id=ei.source_id
            WHERE ds.dashboard_id=:dashboard_id AND ei.quality='settled'
              AND ei.interval_start >= :start AND ei.interval_end <= :end
              AND ((ds.code='octopus_import' AND ei.metric_code='grid_import_energy')
                OR (ds.code='octopus_export' AND ei.metric_code='grid_export_energy'))
            ORDER BY ei.interval_start, ei.interval_end
        """), {"dashboard_id": dashboard_id, "start": start, "end": end}).mappings().all()
    imported = [row for row in readings if row.code == "octopus_import"]
    exported = [row for row in readings if row.code == "octopus_export"]
    if not imported and not exported:
        raise HTTPException(409, "No settled Octopus electricity intervals are available for that period")
    if enabled:
        adjusted_imported, simulated_exported, dispatched_kwh = _apply_evening_battery_dispatch(imported, capacity_kwh, discharge_percent, discharge_start, discharge_end)
    else:
        adjusted_imported, simulated_exported, dispatched_kwh = imported, [], Decimal("0")
    adjusted_exported = [*exported, *simulated_exported]
    # Keep the settled interval profile visible in the comparison response so
    # the UI can explain *why* a tariff wins, not only show its final total.
    imports_by_day: dict[date, list] = defaultdict(list)
    exports_by_day: dict[date, list] = defaultdict(list)
    daily_profile: dict[date, dict[str, Decimal]] = defaultdict(lambda: {"import_kwh": Decimal("0"), "export_kwh": Decimal("0")})
    usage_periods = {
        "overnight": {"label": "Overnight", "hours": "00:00–06:00", "kwh": Decimal("0")},
        "daytime": {"label": "Daytime", "hours": "06:00–16:00", "kwh": Decimal("0")},
        "peak": {"label": "Peak", "hours": "16:00–19:00", "kwh": Decimal("0")},
        "evening": {"label": "Evening", "hours": "19:00–00:00", "kwh": Decimal("0")},
    }
    for row in adjusted_imported:
        local = row.interval_start.astimezone(finance.LONDON)
        imports_by_day[local.date()].append(row)
        daily_profile[local.date()]["import_kwh"] += row.value_kwh
        period = "overnight" if local.hour < 6 else "daytime" if local.hour < 16 else "peak" if local.hour < 19 else "evening"
        usage_periods[period]["kwh"] += row.value_kwh
    for row in adjusted_exported:
        local = row.interval_start.astimezone(finance.LONDON)
        exports_by_day[local.date()].append(row)
        daily_profile[local.date()]["export_kwh"] += row.value_kwh
    client = OctopusClient(config["OCTOPUS_API_KEY"])
    comparison = []
    candidate_daily: list[dict[date, dict[str, str | None]]] = []
    for candidate in candidates:
        name = str(candidate.get("name") or "Tariff option").strip()[:80]
        import_code = str(candidate.get("import_tariff_code") or "").strip().upper()[:160]
        export_code = str(candidate.get("export_tariff_code") or "").strip().upper()[:160]
        if not import_code or not export_code:
            comparison.append({"name": name, "import_tariff_code": import_code or None, "export_tariff_code": export_code or None, "import_cost_gbp": None, "export_credit_gbp": None, "standing_charge_gbp": None, "net_position_gbp": None, "reason": "Add both an import and an export tariff code"})
            candidate_daily.append({})
            continue
        try:
            import_curve = _tariff_curve(client, import_code, start, end)
            export_curve = _tariff_curve(client, export_code, start, end)
            standing_curve = _tariff_curve(client, import_code, start, end, standing=True)
        except OctopusApiError as exc:
            comparison.append({"name": name, "import_tariff_code": import_code, "export_tariff_code": export_code, "import_cost_gbp": None, "export_credit_gbp": None, "standing_charge_gbp": None, "net_position_gbp": None, "reason": str(exc)})
            candidate_daily.append({})
            continue
        import_cost = _tariff_comparison_value(adjusted_imported, import_curve)
        export_credit = _tariff_comparison_value(adjusted_exported, export_curve)
        standing = Decimal("0")
        complete_standing = True
        day = start_day
        while day < end_day:
            value = standing_curve.at(finance.midnight(day))
            if value is None:
                complete_standing = False
                break
            standing += value / Decimal("100")
            day += timedelta(days=1)
        net = None if import_cost is None or export_credit is None or not complete_standing else export_credit - import_cost - standing
        comparison.append({
            "name": name, "import_tariff_code": import_code, "export_tariff_code": export_code,
            "import_cost_gbp": finance.money(import_cost), "export_credit_gbp": finance.money(export_credit),
            "standing_charge_gbp": finance.money(standing) if complete_standing else None,
            "net_position_gbp": finance.money(net),
            "reason": None if net is not None else "Octopus has no published rate for part of this period",
        })
        daily_values: dict[date, dict[str, str | None]] = {}
        for local_day in daily_profile:
            day_import = _tariff_comparison_value(imports_by_day[local_day], import_curve) if imports_by_day[local_day] else Decimal("0")
            day_export = _tariff_comparison_value(exports_by_day[local_day], export_curve) if exports_by_day[local_day] else Decimal("0")
            day_standing_pence = standing_curve.at(finance.midnight(local_day))
            day_standing = day_standing_pence / Decimal("100") if day_standing_pence is not None else None
            day_net = None if day_import is None or day_export is None or day_standing is None else day_export - day_import - day_standing
            daily_values[local_day] = {"import_cost_gbp": finance.money(day_import), "export_credit_gbp": finance.money(day_export), "standing_charge_gbp": finance.money(day_standing), "net_position_gbp": finance.money(day_net)}
        candidate_daily.append(daily_values)
    total_profile_kwh = sum((value["import_kwh"] for value in daily_profile.values()), Decimal("0"))
    insights = {"usage_periods": [{"key": key, "label": value["label"], "hours": value["hours"], "kwh": str(value["kwh"]), "percent": str((value["kwh"] / total_profile_kwh * Decimal("100")) if total_profile_kwh else Decimal("0"))} for key, value in usage_periods.items()]}
    daily = [{"date": local_day.isoformat(), "import_kwh": str(profile["import_kwh"]), "export_kwh": str(profile["export_kwh"]), "candidates": [values.get(local_day) for values in candidate_daily]} for local_day, profile in sorted(daily_profile.items())]
    return {"start": start_day.isoformat(), "end": end_day.isoformat(), "settled_import_kwh": str(sum((row.value_kwh for row in imported), Decimal("0"))), "settled_export_kwh": str(sum((row.value_kwh for row in exported), Decimal("0"))), "simulated_export_kwh": str(sum((row.value_kwh for row in simulated_exported), Decimal("0"))), "battery_dispatch": {"enabled": enabled, "usable_capacity_kwh": str(capacity_kwh) if capacity_kwh is not None else None, "discharge_percent": str(discharge_percent), "start_time": discharge_start, "end_time": discharge_end, "dispatched_kwh": str(dispatched_kwh), "modelled": enabled}, "candidates": comparison, "insights": insights, "daily": daily}

@app.put("/api/setup")
async def setup_save(request: Request, identity: dict = Depends(auth.current_identity)):
    if not identity.get("is_owner", False):
        raise HTTPException(403, "Only the dashboard owner can access dashboard settings")
    auth.require_csrf(request, identity); payload = await request.json()
    start = payload.get("system_start_at"); projections = payload.get("projections", []); credentials = payload.get("credentials", {})
    if not isinstance(start, str) or not start: raise HTTPException(422, "A system start date is required")
    if not isinstance(projections, list) or not isinstance(credentials, dict): raise HTTPException(422, "Invalid setup payload")
    earnings = payload.get("earnings_config") or {}
    if not isinstance(earnings, dict): raise HTTPException(422, "Invalid electricity rate settings")
    flat_rates = []
    for direction, key in (("import", "import_rates"), ("export", "export_rates")):
        for row in earnings.get(key, []):
            if not isinstance(row, dict):
                raise HTTPException(422, "Each electricity rate must be an object")
            try:
                start_at = datetime.fromisoformat(str(row["start_date"]))
                end_at = datetime.fromisoformat(str(row["end_date"]))
                if end_at < start_at: raise ValueError
            except (KeyError, TypeError, ValueError):
                raise HTTPException(422, "Each electricity rate requires valid start and end dates")
            if row.get("kind") == "flat":
                try:
                    rate = float(row["pence"])
                    if rate < 0: raise ValueError
                except (KeyError, TypeError, ValueError): raise HTTPException(422, "Flat rates require a numeric p/kWh value")
                flat_rates.append((direction, finance.midnight(start_at.date()), finance.midnight(end_at.date() + timedelta(days=1)), rate))
    required = {"octopus": ("api_key", "account_number"), "ecoflow_developer": ("access_key", "secret_key", "serial"), "ecoflow_app": ("email", "password")}
    if not identity["setup_complete"]:
        for provider, fields in required.items():
            value = credentials.get(provider)
            if not isinstance(value, dict) or any(not str(value.get(field) or "").strip() for field in fields):
                raise HTTPException(422, f"{provider.replace('_', ' ')} credentials are required")
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE dashboard_accounts SET display_name=:name,timezone='Europe/London',updated_at=now() WHERE id=:id"), {"id":identity["dashboard_id"],"name":str(payload.get("display_name") or "Home energy")[:128]})
        conn.execute(text("UPDATE dashboard_settings SET system_start_at=:start,earnings_config=CAST(:earnings AS jsonb),setup_complete=true,updated_at=now() WHERE dashboard_id=:id"), {"id":identity["dashboard_id"],"start":start,"earnings":json.dumps(payload.get("earnings_config") or {})})
        conn.execute(text("DELETE FROM tariff_rates WHERE dashboard_id=:id AND origin='dashboard_flat_rate'"), {"id": identity["dashboard_id"]})
        for direction, start_at, end_at, rate in flat_rates:
            conn.execute(text("""INSERT INTO tariff_rates(dashboard_id,fuel,direction,tariff_code,rate_kind,valid_from,valid_to,value_inc_vat_pence,value_exc_vat_pence,origin)
              VALUES (:id,'electricity',:direction,'dashboard-flat','unit_rate',:start,:end,:rate,:rate,'dashboard_flat_rate')"""), {"id":identity["dashboard_id"],"direction":direction,"start":start_at,"end":end_at,"rate":rate})
        conn.execute(text("DELETE FROM projection_targets WHERE dashboard_id=:id"), {"id":identity["dashboard_id"]})
        start_year = datetime.fromisoformat(start).year
        for item in projections:
            if not isinstance(item, dict):
                raise HTTPException(422, "Each projection must be an object")
            try:
                # `year` is a friendly one-based system-year index used by the
                # setup form.  `fiscal_year` remains supported for API users.
                year = int(item["fiscal_year"]) if "fiscal_year" in item else start_year + int(item["year"]) - 1
                values = {"dashboard":identity["dashboard_id"],"year":year,"generation":item["generation_kwh"],"solar":item["solar_gbp"],"battery":item["battery_gbp"],"export":item["export_gbp"],"cost":item.get("cost_gbp"),"net_position":item.get("net_position_gbp")}
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(422, "Each projection needs year, generation_kwh, solar_gbp, battery_gbp, and export_gbp") from exc
            conn.execute(text("""INSERT INTO projection_targets(dashboard_id,fiscal_year,generation_kwh,solar_gbp,battery_gbp,export_gbp,cost_gbp,net_position_gbp)
              VALUES (:dashboard,:year,:generation,:solar,:battery,:export,:cost,:net_position)"""), values)
        for provider, value in credentials.items():
            if provider not in {"octopus","ecoflow_developer","ecoflow_app","axle_vpp"} or not isinstance(value, dict): continue
            if not any(str(item or "").strip() for item in value.values()):
                continue
            ciphertext, nonce = auth.encrypt_credentials(str(identity["dashboard_id"]), provider, value)
            conn.execute(text("INSERT INTO provider_credentials(dashboard_id,provider,ciphertext,nonce) VALUES (:dashboard,:provider,:ciphertext,:nonce) ON CONFLICT(dashboard_id,provider) DO UPDATE SET ciphertext=excluded.ciphertext,nonce=excluded.nonce,updated_at=now()"), {"dashboard":identity["dashboard_id"],"provider":provider,"ciphertext":ciphertext,"nonce":nonce})
    finance.invalidate_cache()
    CatalogRepository(get_engine()).seed(str(identity["dashboard_id"]))
    return {"saved": True}


@app.get("/api/health/live")
def liveness():
    return {"status": "alive"}


@app.get("/api/health/ready")
def readiness():
    ready = database_ready()
    return JSONResponse(
        {"status": "ready" if ready else "unavailable"},
        status_code=200 if ready else 503,
    )


def _read_status_for_dashboard(dashboard_id: str) -> SystemStatus:
    """Read provider status in the selected dashboard's tenant scope."""
    scope = tenant.set_dashboard(dashboard_id)
    try:
        return read_status()
    finally:
        tenant.reset_dashboard(scope)


@app.get("/api/status", response_model=SystemStatus)
def status(request: Request):
    # Status is also used before login, so it remains a public endpoint.  When
    # a session has selected a dashboard, expose that dashboard's provider
    # status rather than querying with an empty tenant scope.
    try:
        identity = auth.current_identity(request)
    except HTTPException:
        return read_status()
    if identity.get("selection_required"):
        return read_status()
    return _read_status_for_dashboard(str(identity["dashboard_id"]))




@app.get("/api/dashboard/overview")
def dashboard_overview(date: date, details: bool = True):
    if not database_ready(): return JSONResponse({"detail":"database unavailable"},status_code=503)
    return dashboard.overview(date, include_details=details)


@app.get("/api/dashboard/overview/details/{panel}")
def dashboard_overview_details(panel: str, date: date):
    if not database_ready(): return JSONResponse({"detail": "database unavailable"}, status_code=503)
    try:
        return dashboard.overview_details(date, panel)
    except ValueError:
        raise HTTPException(404, "unknown overview detail panel")

@app.get("/api/dashboard/history")
def dashboard_history(date: date, period: str = "day"):
    if period not in {"day","week","month"}: raise HTTPException(422,"period must be day, week, or month")
    if not database_ready(): return JSONResponse({"detail":"database unavailable"},status_code=503)
    payload = dashboard.history(date,period)
    payload["costs"] = dashboard.costs(date,period)
    return payload

@app.get("/api/dashboard/rates")
def dashboard_rates(date: date):
    if not database_ready(): return JSONResponse({"detail": "database unavailable"}, status_code=503)
    return dashboard.octopus_rates(date)

@app.get("/api/dashboard/daily-earnings")
def dashboard_daily_earnings(start: date = date(2026, 3, 15), end: date = date(2026, 9, 26)):
    if not database_ready(): return JSONResponse({"detail": "database unavailable"}, status_code=503)
    if end < start: raise HTTPException(422, "end must not precede start")
    return {"rows": dashboard.daily_earnings(start, end), "fiscal_start_month": dashboard.fiscal_start_month()}

@app.get("/api/dashboard/daily-net-position")
def dashboard_daily_net_position(start: date, end: date, as_of: date | None = None):
    if not database_ready(): return JSONResponse({"detail": "database unavailable"}, status_code=503)
    if end < start: raise HTTPException(422, "end must not precede start")
    dashboard_id = tenant.current_dashboard()

    def scoped(call):
        scope = tenant.set_dashboard(dashboard_id) if dashboard_id else None
        try:
            return call()
        finally:
            if scope is not None:
                tenant.reset_dashboard(scope)

    # The calendar and summary are independent, so panel latency is governed
    # by the slower query instead of their combined duration.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="net-position") as executor:
        rows = executor.submit(scoped, lambda: dashboard.daily_net_positions(start, end))
        summary = executor.submit(scoped, lambda: dashboard.net_position(as_of or end))
        return {"rows": rows.result(), "summary": summary.result(), "fiscal_start_month": dashboard.fiscal_start_month()}

@app.get("/api/dashboard/energy-bills")
def dashboard_energy_bills(year: int):
    if not database_ready(): return JSONResponse({"detail": "database unavailable"}, status_code=503)
    if year < 2000 or year > 2100: raise HTTPException(422, "year must be between 2000 and 2100")
    dashboard_id = tenant.current_dashboard()

    def scoped(call):
        scope = tenant.set_dashboard(dashboard_id) if dashboard_id else None
        try:
            return call()
        finally:
            if scope is not None:
                tenant.reset_dashboard(scope)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="energy-bills") as executor:
        rows = executor.submit(scoped, lambda: dashboard.energy_bills_year(year))
        agreements = executor.submit(scoped, dashboard.current_tariff_agreements)
        return {"rows": rows.result(), "agreements": agreements.result()}
@app.get("/api/operations")
def dashboard_operations():
    if not database_ready(): return JSONResponse({"detail": "database unavailable"}, status_code=503)
    return dashboard.operations()


@app.post("/api/operations/retry-partials")
def retry_partial_imports(request: Request, identity: dict = Depends(auth.current_identity)):
    """Make this dashboard's partial Octopus windows eligible on the next collector run."""
    auth.require_csrf(request, identity)
    with get_engine().begin() as connection:
        connection.execute(text("""UPDATE coverage_windows cw SET recorded_at=CURRENT_TIMESTAMP - interval '7 hours'
          FROM data_sources ds
          WHERE cw.source_id=ds.id AND ds.dashboard_id=:dashboard
            AND ds.code IN ('octopus_import','octopus_export','octopus_gas') AND cw.status='partial'"""), {"dashboard": identity["dashboard_id"]})
    return {"queued": True}


@app.post("/api/internal/collector-log", include_in_schema=False)
async def receive_collector_log(payload: dict):
    """Keep recent collector output in memory for the Logs page only."""
    dashboard_id, line = payload.get("dashboard_id"), payload.get("line")
    if isinstance(dashboard_id, str) and isinstance(line, str) and line:
        collector_logs[dashboard_id].append(line[:2000])
    return {"accepted": True}


@app.get("/api/collector-logs")
def get_collector_logs(identity: dict = Depends(auth.current_identity)):
    return {"lines": list(collector_logs.get(str(identity["dashboard_id"]), ()))}

@app.get("/api/telemetry/live")
def live_telemetry(identity: dict = Depends(auth.current_identity)):
    if not database_ready(): return JSONResponse({"metrics": {}}, status_code=503)
    approved = {"octopus_mini_consumption_power", "octopus_mini_export_power", "octopus_mini_demand_power", "octopus_mini_consumption_delta", "octopus_mini_cost_delta", "octopus_mini_cost_delta_inc_vat", "solar_power", "home_power", "grid_power", "battery_power", "battery_soc", "ecoflow_battery_pack_count", "ecoflow_battery_capacity_wh", "ecoflow_operating_mode", "ecoflow_inverter_status", "ecoflow_grid_connection"}
    metrics = {}
    now = datetime.now(timezone.utc)
    repository_scope = str(identity["dashboard_id"])
    health = LiveProviderHealthRepository(get_engine(), repository_scope).get("ecoflow_powerocean", "telemetry")
    for source in ("octopus_mini", "ecoflow_powerocean"):
        for item in TelemetryRepository(get_engine(), repository_scope).latest(source):
            if item.metric_code in approved or item.metric_code.startswith("ecoflow_pack_") or item.metric_code == "ecoflow_inverter_serial":
                value = item.value_number if item.value_number is not None else item.value_text if item.value_text is not None else item.value_boolean
                metrics[f"{source}:{item.metric_code}"] = {"value": str(value), "sampled_at": item.sampled_at.isoformat(), "age_seconds": max(0, int((now-item.sampled_at).total_seconds())), "unit": {"octopus_mini_consumption_delta":"kWh", "octopus_mini_cost_delta":"pence", "octopus_mini_cost_delta_inc_vat":"pence", "battery_soc":"%"}.get(item.metric_code, None if item.metric_code.startswith("ecoflow_") else "W"), "transport": health.transport if source == "ecoflow_powerocean" and health else None}
    return {"metrics": metrics, "ecoflow_transport": health.transport if health else None}

@app.get("/api/events")
async def events(request: Request):
    identity = auth.current_identity(request)
    async def snapshots():
        try:
            last_live = None
            tick = 0
            while not await request.is_disconnected():
                snapshot = await asyncio.to_thread(_read_status_for_dashboard, str(identity["dashboard_id"]))
                if tick % 5 == 0: yield f"event: status\ndata: {snapshot.model_dump_json()}\n\n"
                live = await asyncio.to_thread(live_telemetry, identity)
                encoded = __import__('json').dumps(live)
                if encoded != last_live:
                    yield f"event: telemetry\ndata: {encoded}\n\n"; last_live = encoded
                tick += 1; await asyncio.sleep(1)
        except asyncio.CancelledError:
            # StreamingResponse cancels this generator when the browser disconnects.
            raise

    return StreamingResponse(
        snapshots(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )
