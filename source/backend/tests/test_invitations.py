import asyncio
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app import main


@pytest.fixture
def admin(postgres_engine, monkeypatch):
    email = f"admin-{uuid.uuid4().hex}@example.com"
    with postgres_engine.begin() as conn:
        user = conn.execute(text("INSERT INTO users(email,google_subject,is_admin) VALUES (:email,:email,true) RETURNING id"), {"email":email}).scalar_one()
        dashboard = conn.execute(text("INSERT INTO dashboard_accounts(owner_user_id) VALUES (:user) RETURNING id"), {"user":user}).scalar_one()
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", email)
    monkeypatch.setattr(main.auth, "require_csrf", lambda *args: None)
    monkeypatch.setattr(main, "send_invitation", Mock())
    return {"email":email,"user_id":user,"dashboard_id":dashboard,"is_admin":True}


def request(email):
    return Mock(json=AsyncMock(return_value={"email":email,"type":"dashboard_access"}))


@pytest.mark.parametrize("email", ["bad", "a@@example.com", "a b@example.com", "a@example"])
def test_invalid_email_rejected(admin, email):
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.create_invitation(request(email), admin))
    assert error.value.status_code == 422
    main.send_invitation.assert_not_called()


def test_invitation_lifecycle(admin, postgres_engine):
    email = f"guest-{uuid.uuid4().hex}@example.com"
    asyncio.run(main.create_invitation(request(email), admin))
    row = main.list_invitations(admin)["invitations"][0]
    assert (row["email_status"], row["status"], row["can_reinvite"]) == ("Sent", "Pending", False)
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.create_invitation(request(email), admin))
    assert error.value.status_code == 409
    with postgres_engine.begin() as conn:
        conn.execute(text("UPDATE invitations SET expires_at=now() WHERE email=:email"), {"email":email})
    assert main.list_invitations(admin)["invitations"][0]["can_reinvite"] is True
    asyncio.run(main.create_invitation(request(email), admin))
    assert main.list_invitations(admin)["invitations"][0]["can_reinvite"] is False
    assert main.send_invitation.call_count == 2
    with postgres_engine.begin() as conn:
        conn.execute(text("UPDATE invitations SET consumed_at=now(),expires_at=now() WHERE email=:email"), {"email":email})
    assert main.list_invitations(admin)["invitations"][0]["status"] == "Accepted"
    with pytest.raises(HTTPException):
        asyncio.run(main.create_invitation(request(email), admin))
    asyncio.run(main.delete_invitation(request(email), admin))
    assert main.list_invitations(admin)["invitations"] == []


def test_failed_send_can_be_retried(admin):
    email = f"guest-{uuid.uuid4().hex}@example.com"
    main.send_invitation.side_effect = RuntimeError("SMTP unavailable")
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.create_invitation(request(email), admin))
    assert error.value.status_code == 503
    assert main.list_invitations(admin)["invitations"] == []
    main.send_invitation.side_effect = None
    asyncio.run(main.create_invitation(request(email), admin))
    assert len(main.list_invitations(admin)["invitations"]) == 1


def test_delete_scoped_to_inviter(admin):
    email = f"guest-{uuid.uuid4().hex}@example.com"
    asyncio.run(main.create_invitation(request(email), admin))
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.delete_invitation(request(email), {**admin,"user_id":uuid.uuid4()}))
    assert error.value.status_code == 404
    assert len(main.list_invitations(admin)["invitations"]) == 1
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.delete_invitation(request(email), {**admin,"email":"not-the-admin@example.com","is_admin":True}))
    assert error.value.status_code == 403


@pytest.mark.parametrize("other_access", [False, True])
def test_delete_revokes_only_invited_dashboard_access(admin, postgres_engine, other_access):
    from starlette.requests import Request

    email = f"guest-{uuid.uuid4().hex}@example.com"
    token = uuid.uuid4().hex
    asyncio.run(main.create_invitation(request(email), admin))
    with postgres_engine.begin() as conn:
        user = conn.execute(text("INSERT INTO users(email,google_subject) VALUES (:email,:email) RETURNING id"), {"email":email}).scalar_one()
        conn.execute(text("INSERT INTO dashboard_settings(dashboard_id) VALUES (:dashboard)"), {"dashboard":admin["dashboard_id"]})
        conn.execute(text("INSERT INTO dashboard_memberships(dashboard_id,user_id) VALUES (:dashboard,:user)"), {"dashboard":admin["dashboard_id"],"user":user})
        conn.execute(text("UPDATE invitations SET consumed_at=now() WHERE email=:email"), {"email":email})
        conn.execute(text("INSERT INTO user_sessions(token_hash,user_id,csrf_token,expires_at) VALUES (:token,:user,'csrf',now()+interval '1 day')"), {"token":main.auth._hash(token),"user":user})
        if other_access:
            other = conn.execute(text("INSERT INTO dashboard_accounts(owner_user_id) VALUES (:user) RETURNING id"), {"user":user}).scalar_one()
            conn.execute(text("INSERT INTO dashboard_settings(dashboard_id) VALUES (:dashboard)"), {"dashboard":other})
            conn.execute(text("INSERT INTO dashboard_memberships(dashboard_id,user_id) VALUES (:dashboard,:user)"), {"dashboard":other,"user":user})
    session_request = Request({"type":"http","headers":[(b"cookie", f"energy_session={token}".encode())]})
    asyncio.run(main.delete_invitation(request(email), admin))
    with postgres_engine.connect() as conn:
        memberships = conn.execute(text("SELECT dashboard_id FROM dashboard_memberships WHERE user_id=:user"), {"user":user}).scalars().all()
        assert admin["dashboard_id"] not in memberships
        assert memberships == ([other] if other_access else [])
        assert conn.execute(text("SELECT id FROM users WHERE id=:user"), {"user":user}).scalar_one() == user
    if other_access:
        assert main.auth.current_identity(session_request)["dashboard_id"] == other
    else:
        with pytest.raises(HTTPException) as error:
            main.auth.current_identity(session_request)
        assert error.value.status_code == 401


def test_delete_cannot_revoke_owner(admin):
    asyncio.run(main.create_invitation(request(admin["email"]), admin))
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.delete_invitation(request(admin["email"]), admin))
    assert error.value.status_code == 403
    assert len(main.list_invitations(admin)["invitations"]) == 1


@pytest.mark.parametrize("accepted", [False, True])
def test_delete_new_dashboard_invitation(admin, postgres_engine, accepted):
    email = f"new-{uuid.uuid4().hex}@example.com"
    req = Mock(json=AsyncMock(return_value={"email":email,"type":"new_dashboard"}))
    asyncio.run(main.create_invitation(req, admin))
    if accepted:
        with postgres_engine.begin() as conn:
            user = conn.execute(text("INSERT INTO users(email,google_subject) VALUES (:email,:email) RETURNING id"), {"email":email}).scalar_one()
            dashboard = conn.execute(text("INSERT INTO dashboard_accounts(owner_user_id) VALUES (:user) RETURNING id"), {"user":user}).scalar_one()
            conn.execute(text("INSERT INTO dashboard_settings(dashboard_id) VALUES (:id)"), {"id":dashboard})
            conn.execute(text("INSERT INTO dashboard_memberships(dashboard_id,user_id) VALUES (:id,:user)"), {"id":dashboard,"user":user})
            conn.execute(text("UPDATE invitations SET consumed_at=now() WHERE email=:email"), {"email":email})
        main.CatalogRepository(postgres_engine).seed(str(dashboard))
        with postgres_engine.begin() as conn:
            source = conn.execute(text("SELECT id FROM data_sources WHERE dashboard_id=:id LIMIT 1"), {"id":dashboard}).scalar_one()
            conn.execute(text("INSERT INTO ingestion_runs(source_id,dataset,status) VALUES (:source,'test','complete')"), {"source":source})
            conn.execute(text("INSERT INTO energy_intervals(source_id,metric_code,interval_start,interval_end,value_kwh,quality,completeness) VALUES (:source,'grid_import_energy',now(),now()+interval '30 minutes',1,'settled','complete')"), {"source":source})
            conn.execute(text("INSERT INTO sync_checkpoints(source_id,dataset,last_success_at) VALUES (:source,'test',now())"), {"source":source})
            conn.execute(text("INSERT INTO provider_credentials(dashboard_id,provider,ciphertext,nonce) VALUES (:id,'octopus',:data,:data)"), {"id":dashboard,"data":b'test'})
    assert len(main.list_invitations(admin, "new_dashboard")["invitations"]) == 1
    assert main.list_invitations(admin, "dashboard_access")["invitations"] == []
    asyncio.run(main.delete_invitation(req, admin))
    assert main.list_invitations(admin)["invitations"] == []
    with postgres_engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM dashboard_accounts WHERE id=:id"), {"id":admin["dashboard_id"]}).scalar_one() == admin["dashboard_id"]
        if accepted:
            for table in ("dashboard_settings", "dashboard_memberships", "provider_credentials", "data_sources"):
                assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE dashboard_id=:id"), {"id":dashboard}).scalar_one() == 0
            for table in ("energy_intervals", "ingestion_runs", "sync_checkpoints"):
                assert conn.execute(text(f"SELECT count(*) FROM {table} WHERE source_id=:id"), {"id":source}).scalar_one() == 0
            assert conn.execute(text("SELECT id FROM dashboard_accounts WHERE id=:id"), {"id":dashboard}).scalar() is None


def test_new_dashboard_delete_protects_admin(admin):
    req = Mock(json=AsyncMock(return_value={"email":admin["email"],"type":"new_dashboard"}))
    asyncio.run(main.create_invitation(req, admin))
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.delete_invitation(req, admin))
    assert error.value.status_code == 403
    assert len(main.list_invitations(admin)["invitations"]) == 1


@pytest.mark.parametrize("configured,stored_admin,allowed", [
    ("match", False, True),
    ("mixed_case", True, True),
    ("different@example.com", True, False),
    ("", True, False),
])
def test_invitation_access_uses_configured_email(admin, monkeypatch, configured, stored_admin, allowed):
    value = admin["email"] if configured == "match" else f"  {admin['email'].upper()}  " if configured == "mixed_case" else configured
    monkeypatch.setenv("INITIAL_ADMIN_EMAIL", value)
    identity = {**admin,"is_admin":stored_admin,"is_owner":True,"display_name":"Test","setup_complete":True,"system_start_at":None}
    assert main.auth_me(identity)["user"]["admin"] is allowed
    email = f"guest-{uuid.uuid4().hex}@example.com"
    if allowed:
        asyncio.run(main.create_invitation(request(email), identity))
        assert len(main.list_invitations(identity)["invitations"]) == 1
        asyncio.run(main.delete_invitation(request(email), identity))
    else:
        for operation in (
            lambda: main.list_invitations(identity),
            lambda: asyncio.run(main.create_invitation(request(email), identity)),
            lambda: asyncio.run(main.delete_invitation(request(email), identity)),
        ):
            with pytest.raises(HTTPException) as error:
                operation()
            assert error.value.status_code == 403
        main.send_invitation.assert_not_called()


@pytest.mark.parametrize("first_type", ["dashboard_access", "new_dashboard"])
def test_existing_user_can_own_and_view_dashboards(admin, postgres_engine, first_type):
    from fastapi.testclient import TestClient

    email = f"multi-{uuid.uuid4().hex}@example.com"
    google = {"email":email,"subject":uuid.uuid4().hex,"name":"Multi Dashboard User"}
    def invite(kind):
        return Mock(json=AsyncMock(return_value={"email":email,"type":kind}))
    with postgres_engine.begin() as conn:
        conn.execute(text("INSERT INTO dashboard_settings(dashboard_id,setup_complete) VALUES (:id,true)"), {"id":admin["dashboard_id"]})
    asyncio.run(main.create_invitation(invite(first_type), admin))
    main.auth.create_or_login(google)
    second_type = "new_dashboard" if first_type == "dashboard_access" else "dashboard_access"
    asyncio.run(main.create_invitation(invite(second_type), admin))
    token, csrf = main.auth.create_or_login(google)
    assert all(item["status"] == "Accepted" for item in main.list_invitations(admin)["invitations"])
    with TestClient(main.app) as client:
        client.cookies.set(main.auth.SESSION_COOKIE, token)
        me = client.get("/api/auth/me").json()
        assert me["selection_required"] is True
        assert len(me["dashboards"]) == 2
        owner_dashboard = next(item["id"] for item in me["dashboards"] if item["is_owner"])
        assert client.get("/api/setup").status_code == 409
        assert client.post("/api/auth/dashboard", json={"dashboard_id":str(uuid.uuid4())}).status_code == 403
        assert client.post("/api/auth/dashboard", json={"dashboard_id":str(admin["dashboard_id"])}).status_code == 200
        assert client.get("/api/auth/me").json()["dashboard"]["is_owner"] is False
        assert client.get("/api/setup").status_code == 403
        assert client.post("/api/auth/dashboard", json={"dashboard_id":owner_dashboard}).status_code == 200
        assert client.get("/api/auth/me").json()["dashboard"]["is_owner"] is True
        assert client.get("/api/setup").status_code == 200
        # A new login presents the selector again.
        next_token, _ = main.auth.create_or_login(google)
        client.cookies.set(main.auth.SESSION_COOKIE, next_token)
        assert client.get("/api/auth/me").json()["selection_required"] is True
        # Deleting shared access preserves the user's owned dashboard and invite.
        asyncio.run(main.delete_invitation(invite("dashboard_access"), admin))
        me = client.get("/api/auth/me").json()
        assert len(me["dashboards"]) == 1
        assert me["dashboard"]["id"] == owner_dashboard
        assert me["selection_required"] is False
        assert [item["type"] for item in main.list_invitations(admin)["invitations"]] == ["new_dashboard"]


def test_pending_invitations_have_independent_cooldowns(admin):
    email = f"pending-{uuid.uuid4().hex}@example.com"
    for kind in ("new_dashboard", "dashboard_access"):
        req = Mock(json=AsyncMock(return_value={"email":email,"type":kind}))
        asyncio.run(main.create_invitation(req, admin))
        with pytest.raises(HTTPException) as error:
            asyncio.run(main.create_invitation(req, admin))
        assert error.value.status_code == 409
    assert len(main.list_invitations(admin)["invitations"]) == 2


def test_viewer_invitations_are_scoped_per_dashboard(admin, postgres_engine):
    email = f"viewer-{uuid.uuid4().hex}@example.com"
    with postgres_engine.begin() as conn:
        owner_email = f"owner-{uuid.uuid4().hex}@example.com"
        owner = conn.execute(text("INSERT INTO users(email,google_subject) VALUES (:email,:email) RETURNING id"), {"email":owner_email}).scalar_one()
        second_dashboard = conn.execute(text("INSERT INTO dashboard_accounts(owner_user_id) VALUES (:owner) RETURNING id"), {"owner":owner}).scalar_one()
    second = {**admin,"dashboard_id":second_dashboard}
    for identity in (admin, second):
        asyncio.run(main.create_invitation(request(email), identity))
        rows = main.list_invitations(identity, "dashboard_access")["invitations"]
        assert len(rows) == 1
        assert rows[0]["status"] == "Pending"
    with postgres_engine.begin() as conn:
        conn.execute(text("UPDATE invitations SET expires_at=now() WHERE email=:email AND dashboard_id=:dashboard"), {"email":email,"dashboard":admin["dashboard_id"]})
    assert main.list_invitations(admin, "dashboard_access")["invitations"][0]["can_reinvite"] is True
    assert main.list_invitations(second, "dashboard_access")["invitations"][0]["can_reinvite"] is False
    asyncio.run(main.create_invitation(request(email), admin))
    with pytest.raises(HTTPException) as error:
        asyncio.run(main.create_invitation(request(email), second))
    assert error.value.status_code == 409
    main.auth.create_or_login({"email":email,"subject":uuid.uuid4().hex,"name":"Viewer"})
    for identity in (admin, second):
        assert main.list_invitations(identity, "dashboard_access")["invitations"][0]["status"] == "Accepted"
    asyncio.run(main.delete_invitation(request(email), admin))
    assert main.list_invitations(admin, "dashboard_access")["invitations"] == []
    assert main.list_invitations(second, "dashboard_access")["invitations"][0]["status"] == "Accepted"
    with postgres_engine.connect() as conn:
        memberships = conn.execute(text("SELECT m.dashboard_id FROM dashboard_memberships m JOIN users u ON u.id=m.user_id WHERE u.email=:email"), {"email":email}).scalars().all()
        assert memberships == [second_dashboard]
