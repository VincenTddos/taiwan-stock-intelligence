from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.platform import AuditLog

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
ME = "/api/v1/auth/me"


async def _token(client, email: str, password: str) -> str:
    resp = await client.post(LOGIN, json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return str(resp.json()["data"]["access_token"])


async def test_login_success_returns_token_pair(client, admin_user):
    resp = await client.post(
        LOGIN, json={"email": "admin@test.dev", "password": "admin-password-123"}
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["access_token"] and data["refresh_token"]
    assert data["token_type"] == "bearer"
    assert data["access_token"] != data["refresh_token"]


async def test_login_wrong_password_rejected(client, admin_user):
    resp = await client.post(
        LOGIN, json={"email": "admin@test.dev", "password": "wrong-password-xx"}
    )
    assert resp.status_code == 401
    problem = resp.json()
    assert problem["type"].endswith("/authentication-failed")
    assert problem["status"] == 401
    assert "request_id" in problem


async def test_login_unknown_user_gives_identical_error(client, admin_user):
    """Error text must not distinguish 'no such user' from 'wrong password'."""
    a = await client.post(LOGIN, json={"email": "admin@test.dev", "password": "wrong-password-xx"})
    b = await client.post(LOGIN, json={"email": "nobody@test.dev", "password": "wrong-password-xx"})
    assert a.status_code == b.status_code == 401
    assert a.json()["detail"] == b.json()["detail"]


async def test_inactive_user_cannot_login(client, session, viewer_user):
    viewer_user.is_active = False
    await session.commit()
    resp = await client.post(
        LOGIN, json={"email": "viewer@test.dev", "password": "viewer-password-123"}
    )
    assert resp.status_code == 401


async def test_protected_route_requires_token(client, admin_user):
    assert (await client.get(ME)).status_code == 401


async def test_protected_route_rejects_garbage_token(client, admin_user):
    resp = await client.get(ME, headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


async def test_protected_route_with_valid_token(client, admin_user):
    token = await _token(client, "admin@test.dev", "admin-password-123")
    resp = await client.get(ME, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["data"]["email"] == "admin@test.dev"
    assert resp.json()["data"]["role"] == "admin"
    assert "password_hash" not in resp.json()["data"]


async def test_rbac_admin_route_allows_admin(client, admin_user):
    token = await _token(client, "admin@test.dev", "admin-password-123")
    resp = await client.get("/api/v1/auth/admin-only", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200


async def test_rbac_admin_route_denies_viewer(client, viewer_user):
    token = await _token(client, "viewer@test.dev", "viewer-password-123")
    resp = await client.get("/api/v1/auth/admin-only", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["type"].endswith("/permission-denied")


async def test_refresh_rotates_and_revokes_old_token(client, redis_client, admin_user):
    login = await client.post(
        LOGIN, json={"email": "admin@test.dev", "password": "admin-password-123"}
    )
    old_refresh = login.json()["data"]["refresh_token"]

    first = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert first.status_code == 200
    assert first.json()["data"]["refresh_token"] != old_refresh

    # Replaying the consumed token must fail — this is what makes theft detectable.
    replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": old_refresh})
    assert replay.status_code == 401
    assert "revoked" in replay.json()["detail"].lower()


async def test_logout_revokes_refresh_token(client, redis_client, admin_user):
    login = await client.post(
        LOGIN, json={"email": "admin@test.dev", "password": "admin-password-123"}
    )
    tokens = login.json()["data"]

    logout = await client.post(
        "/api/v1/auth/logout",
        json={"refresh_token": tokens["refresh_token"]},
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert logout.status_code == 204

    resp = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert resp.status_code == 401


async def test_login_writes_audit_log(client, session, admin_user):
    await client.post(LOGIN, json={"email": "admin@test.dev", "password": "admin-password-123"})
    await client.post(LOGIN, json={"email": "admin@test.dev", "password": "bad-password-1234"})

    rows = (await session.execute(select(AuditLog).order_by(AuditLog.id))).scalars().all()
    results = [(r.action, r.result) for r in rows]
    assert ("login", "SUCCESS") in results
    assert ("login", "DENIED") in results


async def test_validation_error_shape(client):
    resp = await client.post(LOGIN, json={"email": "not-an-email", "password": "x"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["type"].endswith("/validation-error")
    assert isinstance(body["errors"], list) and body["errors"]
