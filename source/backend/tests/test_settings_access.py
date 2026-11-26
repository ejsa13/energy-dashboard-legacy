import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app import auth
from app.main import app


@pytest.mark.parametrize("owner,admin", [(True, False), (False, False), (False, True)])
def test_settings_require_dashboard_ownership(postgres_engine, owner, admin):
    token = uuid.uuid4().hex
    csrf = uuid.uuid4().hex
    with postgres_engine.begin() as conn:
        users = []
        for is_admin in (False, admin):
            email = f"{uuid.uuid4().hex}@example.com"
            users.append(conn.execute(text("INSERT INTO users(email,google_subject,is_admin) VALUES (:email,:email,:admin) RETURNING id"), {"email":email,"admin":is_admin}).scalar_one())
        dashboard = conn.execute(text("INSERT INTO dashboard_accounts(owner_user_id) VALUES (:owner) RETURNING id"), {"owner":users[0]}).scalar_one()
        conn.execute(text("INSERT INTO dashboard_settings(dashboard_id,setup_complete) VALUES (:dashboard,true)"), {"dashboard":dashboard})
        for user in users:
            conn.execute(text("INSERT INTO dashboard_memberships(dashboard_id,user_id) VALUES (:dashboard,:user)"), {"dashboard":dashboard,"user":user})
        conn.execute(text("INSERT INTO user_sessions(token_hash,user_id,csrf_token,expires_at) VALUES (:token,:user,:csrf,now()+interval '1 day')"), {"token":auth._hash(token),"user":users[0] if owner else users[1],"csrf":csrf})
    with TestClient(app) as client:
        client.cookies.set(auth.SESSION_COOKIE, token)
        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["dashboard"]["is_owner"] is owner
        response = client.get("/api/setup")
        assert response.status_code == (200 if owner else 403)
        response = client.put("/api/setup", headers={"X-CSRF-Token":csrf}, json={"display_name":"Updated by owner","system_start_at":"2026-01-01T00:00:00","credentials":{}})
        assert response.status_code == (200 if owner else 403)
    with postgres_engine.connect() as conn:
        name = conn.execute(text("SELECT display_name FROM dashboard_accounts WHERE id=:id"), {"id":dashboard}).scalar_one()
        assert name == ("Updated by owner" if owner else "Home energy")
