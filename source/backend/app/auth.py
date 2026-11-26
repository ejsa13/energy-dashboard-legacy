"""Google OIDC login, invite-only access, sessions, and encrypted setup data."""
from __future__ import annotations

import base64, hashlib, hmac, json, os, secrets, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException, Request
from sqlalchemy import text

from app.database import get_engine

SESSION_COOKIE = "energy_session"
STATE_COOKIE = "energy_oauth_state"

def is_initial_admin(identity: dict[str, Any]) -> bool:
    configured_email = os.environ.get("INITIAL_ADMIN_EMAIL", "").strip().lower()
    return bool(configured_email) and str(identity.get("email") or "").strip().lower() == configured_email


def _hash(value: str) -> str: return hashlib.sha256(value.encode()).hexdigest()
def _now() -> datetime: return datetime.now(timezone.utc)
def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value: raise HTTPException(503, f"{name} is not configured")
    return value
def _key() -> bytes:
    try:
        key = base64.urlsafe_b64decode(_required("DASHBOARD_CREDENTIALS_KEY"))
    except Exception as exc: raise HTTPException(503, "DASHBOARD_CREDENTIALS_KEY is invalid") from exc
    if len(key) != 32: raise HTTPException(503, "DASHBOARD_CREDENTIALS_KEY must decode to 32 bytes")
    return key

def make_oauth_state() -> str:
    nonce = secrets.token_urlsafe(24)
    signature = hmac.new(_required("SESSION_SECRET").encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return f"{nonce}.{signature}"

def valid_oauth_state(value: str) -> bool:
    try:
        nonce, signature = value.rsplit(".", 1)
    except ValueError:
        return False
    expected = hmac.new(_required("SESSION_SECRET").encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)

def google_authorize_url(state: str) -> str:
    callback = _required("PUBLIC_APP_URL").rstrip("/") + "/api/auth/google/callback"
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode({
        "client_id": _required("GOOGLE_CLIENT_ID"), "redirect_uri": callback, "response_type": "code",
        "scope": "openid email profile", "state": state, "prompt": "select_account",
    })

def google_identity(code: str) -> dict[str, str]:
    callback = _required("PUBLIC_APP_URL").rstrip("/") + "/api/auth/google/callback"
    body = urllib.parse.urlencode({"code": code, "client_id": _required("GOOGLE_CLIENT_ID"), "client_secret": _required("GOOGLE_CLIENT_SECRET"), "redirect_uri": callback, "grant_type": "authorization_code"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request("https://oauth2.googleapis.com/token", data=body, headers={"Content-Type":"application/x-www-form-urlencoded"}), timeout=10) as response:
            token = json.load(response)["id_token"]
        with urllib.request.urlopen("https://oauth2.googleapis.com/tokeninfo?" + urllib.parse.urlencode({"id_token":token}), timeout=10) as response:
            claims = json.load(response)
    except Exception as exc: raise HTTPException(401, "Google sign-in could not be verified") from exc
    if claims.get("aud") != _required("GOOGLE_CLIENT_ID") or claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"} or claims.get("email_verified") not in {True, "true"}:
        raise HTTPException(401, "Google identity is not valid for this dashboard")
    return {"subject": str(claims["sub"]), "email": str(claims["email"]).lower(), "name": str(claims.get("name") or claims["email"])}

def create_or_login(identity: dict[str, str]) -> tuple[str, str]:
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    with get_engine().begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:email))"), identity)
        user = conn.execute(text("SELECT id FROM users WHERE google_subject=:subject"), identity).mappings().first()
        invitations = conn.execute(text("SELECT id,dashboard_id FROM invitations WHERE email=:email AND consumed_at IS NULL AND expires_at > now() FOR UPDATE"), identity).mappings().all()
        bootstrap = is_initial_admin(identity)
        if user is None:
            if not invitations and not bootstrap:
                raise HTTPException(403, "This Google account has not been invited")
            user = conn.execute(text("INSERT INTO users(email,google_subject,display_name,is_admin) VALUES (:email,:subject,:name,:admin) RETURNING id"), {**identity,"admin":bootstrap}).mappings().one()
        if bootstrap or any(invitation.dashboard_id is None for invitation in invitations):
            owned = conn.execute(text("INSERT INTO dashboard_accounts(owner_user_id,display_name) VALUES (:user,:name) ON CONFLICT(owner_user_id) DO UPDATE SET owner_user_id=excluded.owner_user_id RETURNING id"), {"user":user.id,"name":f"{identity['name']}'s energy"}).scalar_one()
            conn.execute(text("INSERT INTO dashboard_settings(dashboard_id) VALUES (:dashboard) ON CONFLICT DO NOTHING"), {"dashboard":owned})
            conn.execute(text("INSERT INTO dashboard_memberships(dashboard_id,user_id) VALUES (:dashboard,:user) ON CONFLICT DO NOTHING"), {"dashboard":owned,"user":user.id})
        for invitation in invitations:
            if invitation.dashboard_id:
                conn.execute(text("INSERT INTO dashboard_memberships(dashboard_id,user_id) VALUES (:dashboard,:user) ON CONFLICT DO NOTHING"), {"dashboard":invitation.dashboard_id,"user":user.id})
            conn.execute(text("UPDATE invitations SET consumed_at=now() WHERE id=:id"), {"id":invitation.id})
        conn.execute(text("UPDATE users SET last_login_at=now() WHERE id=:id"), {"id":user.id})
        conn.execute(text("INSERT INTO user_sessions(token_hash,user_id,csrf_token,expires_at) VALUES (:hash,:user,:csrf,:expiry)"), {"hash":_hash(token),"user":user.id,"csrf":csrf,"expiry":_now()+timedelta(days=14)})
    return token, csrf

def current_identity(request: Request) -> dict[str, Any]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token: raise HTTPException(401, "Sign in required")
    with get_engine().connect() as conn:
        rows = conn.execute(text("""SELECT u.id AS user_id,u.email,u.display_name,u.is_admin,
          (d.owner_user_id=u.id) AS is_owner,d.id AS dashboard_id,d.display_name AS dashboard_name,
          s.setup_complete,s.system_start_at,x.selected_dashboard_id
          FROM user_sessions x JOIN users u ON u.id=x.user_id JOIN dashboard_memberships m ON m.user_id=u.id
          JOIN dashboard_accounts d ON d.id=m.dashboard_id JOIN dashboard_settings s ON s.dashboard_id=d.id
          WHERE x.token_hash=:hash AND x.expires_at > now() ORDER BY d.display_name,d.id"""), {"hash":_hash(token)}).mappings().all()
    if not rows: raise HTTPException(401, "Session expired")
    selected = next((row for row in rows if row.dashboard_id == row.selected_dashboard_id), None)
    identity = dict(selected or rows[0])
    identity["selection_required"] = len(rows) > 1 and selected is None
    identity["dashboards"] = [{"id":str(row.dashboard_id),"name":row.dashboard_name,"is_owner":row.is_owner} for row in rows]
    return identity


def require_csrf(request: Request, identity: dict[str, Any]) -> None:
    token = request.headers.get("X-CSRF-Token", "")
    with get_engine().connect() as conn:
        valid = conn.execute(text("SELECT 1 FROM user_sessions WHERE token_hash=:hash AND user_id=:user AND csrf_token=:csrf"), {"hash":_hash(request.cookies.get(SESSION_COOKIE, "")),"user":identity["user_id"],"csrf":token}).scalar()
    if not valid: raise HTTPException(403, "Invalid CSRF token")

def encrypt_credentials(dashboard_id: str, provider: str, payload: dict[str, Any]) -> tuple[bytes, bytes]:
    nonce = os.urandom(12)
    data = AESGCM(_key()).encrypt(nonce, json.dumps(payload).encode(), f"{dashboard_id}:{provider}".encode())
    return data, nonce

def decrypt_credentials(dashboard_id: str, provider: str, ciphertext: bytes, nonce: bytes) -> dict[str, Any]:
    try:
        value = AESGCM(_key()).decrypt(nonce, ciphertext, f"{dashboard_id}:{provider}".encode())
        decoded = json.loads(value)
    except Exception as exc:
        raise HTTPException(500, "Stored provider credentials could not be decrypted") from exc
    if not isinstance(decoded, dict):
        raise HTTPException(500, "Stored provider credentials are invalid")
    return decoded

def collector_configuration(dashboard_id: str) -> dict[str, str] | None:
    """Return one dashboard's decrypted provider settings for collector use.

    This is intentionally collector-only: API responses expose only which
    providers are configured, never these values.
    """
    with get_engine().connect() as conn:
        settings = conn.execute(text("SELECT system_start_at,setup_complete FROM dashboard_settings WHERE dashboard_id=:id"), {"id": dashboard_id}).mappings().first()
        rows = conn.execute(text("SELECT provider,ciphertext,nonce FROM provider_credentials WHERE dashboard_id=:id"), {"id": dashboard_id}).mappings().all()
    if settings is None or not settings.setup_complete:
        return None
    values = {row.provider: decrypt_credentials(dashboard_id, row.provider, bytes(row.ciphertext), bytes(row.nonce)) for row in rows}
    octopus, developer, app = values.get("octopus", {}), values.get("ecoflow_developer", {}), values.get("ecoflow_app", {})
    if not all((octopus.get("api_key"), octopus.get("account_number"), developer.get("access_key"), developer.get("secret_key"), developer.get("serial"), app.get("email"), app.get("password"), settings.system_start_at)):
        return None
    return {
        "OCTOPUS_API_KEY": str(octopus["api_key"]), "OCTOPUS_ACCOUNT_NUMBER": str(octopus["account_number"]),
        "OCTOPUS_MINI_DEVICE_ID": str(octopus.get("mini_device_id") or ""),
        "ECOFLOW_ACCESS_KEY": str(developer["access_key"]), "ECOFLOW_SECRET_KEY": str(developer["secret_key"]),
        "ECOFLOW_POWEROCEAN_SERIAL": str(developer["serial"]), "ECOFLOW_BATTERY_SERIALS": str(developer.get("battery_serials") or ""),
        "ECOFLOW_APP_EMAIL": str(app["email"]), "ECOFLOW_APP_PASSWORD": str(app["password"]),
        "ECOFLOW_START_DATE": settings.system_start_at.isoformat(),
    }
