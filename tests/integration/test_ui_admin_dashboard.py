"""Integration tests for the tabbed admin dashboard (`/admin/*`).

Covers the HTML admin surface end-to-end:
    * tab gates (anonymous 401 / non-admin 403 / admin 200 / unknown 404)
    * signup approve + deny (with reason) via the HTML form
    * user ban / unban / self-ban / admin-ban safety rails
    * quota write with human sizes (`10GiB`) and the `admin.quota` audit
    * GPU write / read / clear with the `admin.gpu` audit (this is the
      end-to-end unblock for GPU assignment from the browser)
    * CSRF on every POST, error-path rollback, nav gating, no inline JS
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from outo_models.db import Approval, AuditLog, User, UserQuota, WebSetting
from outo_models.server.routers._admin_helpers import gpu_setting_key

PASSWORD = "correct horse battery staple"

#: Every POST target the dashboard exposes. The CSRF gate must cover all.
ACTIONS = ("approve", "deny", "ban", "unban", "quota", "gpu")


def _csrf_token(client: TestClient, path: str) -> str:
    """GET `path` and return the CSRF field value matching the cookie."""
    response = client.get(path)
    assert response.status_code == 200, path
    cookie_token = response.cookies.get("_csrf") or client.cookies.get("_csrf")
    assert cookie_token, path
    match = re.search(r'name="_csrf" value="([^"]+)"', response.text)
    assert match is not None
    assert match.group(1) == cookie_token, path
    return cookie_token


def _login(client: TestClient, username: str) -> None:
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, username


async def _seed(
    factory: async_sessionmaker[AsyncSession],
    *,
    username: str,
    status: str,
    role: str = "user",
) -> int:
    """Insert a user with an explicit status (plus quota/usage rows)."""
    from outo_models.auth.passwords import hash_password
    from outo_models.db import UserQuota as _Quota
    from outo_models.db import UserUsage as _Usage

    async with factory() as session:
        user = User(
            username=username,
            email=f"{username}@example.com",
            password_hash=hash_password(PASSWORD),
            role=role,
            status=status,
        )
        session.add(user)
        await session.commit()
        user_id = user.id
        session.add(_Quota(user_id=user_id, max_bytes=10 * 1024**3))
        session.add(_Usage(user_id=user_id, used_bytes=0))
        await session.commit()
        return user_id


async def _status_of(factory: async_sessionmaker[AsyncSession], username: str) -> str | None:
    async with factory() as session:
        user = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        return None if user is None else user.status


async def _audit_actions(factory: async_sessionmaker[AsyncSession], action: str) -> list[AuditLog]:
    async with factory() as session:
        rows = (
            (await session.execute(select(AuditLog).where(AuditLog.action == action)))
            .scalars()
            .all()
        )
        return list(rows)


async def _quota_of(factory: async_sessionmaker[AsyncSession], username: str) -> int | None:
    async with factory() as session:
        user = (await session.execute(select(User).where(User.username == username))).scalar_one()
        row = (
            await session.execute(select(UserQuota).where(UserQuota.user_id == user.id))
        ).scalar_one()
        return row.max_bytes


async def _gpu_of(factory: async_sessionmaker[AsyncSession], username: str) -> str | None:
    async with factory() as session:
        row = (
            await session.execute(
                select(WebSetting).where(WebSetting.key == gpu_setting_key(username))
            )
        ).scalar_one_or_none()
        return None if row is None else row.value


class TestAdminTabGates:
    """Tab routes keep the 401/403/404 contract of the page they serve."""

    async def test_anonymous_users_tab_is_401(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        assert client.get("/admin/users").status_code == 401
        assert client.get("/admin/audit").status_code == 401

    async def test_non_admin_users_tab_is_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        assert client.get("/admin/users").status_code == 403
        assert client.get("/admin/audit").status_code == 403

    async def test_admin_reaches_every_tab(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        _login(client, "root")
        for path in ("/admin", "/admin/signups", "/admin/users", "/admin/audit"):
            assert client.get(path).status_code == 200, path

    async def test_unknown_tab_is_404_json(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        _login(client, "root")
        response = client.get("/admin/bogus")
        assert response.status_code == 404
        assert response.json()["error"] == "not_found"


class TestSignupsTab:
    """The signups tab renders the pending queue and approves / denies."""

    async def test_pending_signup_is_listed(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="newbie", status="pending")
        _login(client, "root")
        response = client.get("/admin/signups")
        assert response.status_code == 200
        assert "newbie" in response.text

    async def test_approve_pends_signup_and_writes_audit(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="newbie", status="pending")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/signups")

        response = client.post(
            "/admin/users/newbie/approve",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/signups?signup_saved=1"
        assert await _status_of(factory, "newbie") == "approved"
        assert len(await _audit_actions(factory, "user.approve")) == 1

    async def test_deny_stores_reason_and_writes_audit(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        alice_id = await _seed(factory, username="newbie", status="pending")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/signups")

        response = client.post(
            "/admin/users/newbie/deny",
            data={"_csrf": csrf, "reason": "duplicate signup"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/signups?signup_saved=1"
        assert await _status_of(factory, "newbie") == "denied"
        async with factory() as session:
            approval = (
                await session.execute(select(Approval).where(Approval.user_id == alice_id))
            ).scalar_one()
            assert approval.reason == "duplicate signup"
        rows = await _audit_actions(factory, "user.deny")
        assert rows and rows[0].detail == "duplicate signup"

    async def test_deny_rejects_oversized_reason_without_mutating(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="newbie", status="pending")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/signups")

        response = client.post(
            "/admin/users/newbie/deny",
            data={"_csrf": csrf, "reason": "x" * 501},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "signup_error=validation_failed" in response.headers["location"]
        assert await _status_of(factory, "newbie") == "pending"
        assert await _audit_actions(factory, "user.deny") == []


class TestUsersTabActions:
    """Ban / unban plus the two ban safety rails."""

    async def test_ban_with_reason_writes_audit(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="alice", status="approved")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/users")

        response = client.post(
            "/admin/users/alice/ban",
            data={"_csrf": csrf, "reason": "abuse"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/users?user_saved=1"
        assert await _status_of(factory, "alice") == "banned"
        rows = await _audit_actions(factory, "user.ban")
        assert rows and rows[0].detail == "abuse"

    async def test_unban_restores_user(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="alice", status="banned")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/users")

        response = client.post(
            "/admin/users/alice/unban", data={"_csrf": csrf}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/users?user_saved=1"
        assert await _status_of(factory, "alice") == "approved"
        assert len(await _audit_actions(factory, "user.unban")) == 1

    async def test_self_ban_is_forbidden_and_changes_nothing(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/users")

        response = client.post(
            "/admin/users/root/ban", data={"_csrf": csrf}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "user_error=forbidden" in response.headers["location"]
        assert await _status_of(factory, "root") == "approved"
        assert await _audit_actions(factory, "user.ban") == []

    async def test_banning_another_admin_is_forbidden(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="root2", status="approved", role="admin")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/users")

        response = client.post(
            "/admin/users/root2/ban", data={"_csrf": csrf}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "user_error=forbidden" in response.headers["location"]
        assert await _status_of(factory, "root2") == "approved"
        assert await _audit_actions(factory, "user.ban") == []

    async def test_status_filter_narrows_the_list(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="alice", status="approved")
        await _seed(factory, username="bobby", status="banned")
        _login(client, "root")

        response = client.get("/admin/users?status=banned")
        assert response.status_code == 200
        assert "bobby" in response.text
        assert ">alice<" not in response.text

    async def test_quota_editor_writes_human_size(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="alice", status="approved")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/users")

        response = client.post(
            "/admin/users/alice/quota",
            data={"_csrf": csrf, "max_bytes": "10GiB"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/users?quota_saved=1"
        assert await _quota_of(factory, "alice") == 10 * 1024**3
        rows = await _audit_actions(factory, "admin.quota")
        assert len(rows) == 1
        detail = json.loads(rows[0].detail)
        assert detail["new_max_bytes"] == 10 * 1024**3
        assert detail["old_max_bytes"] == 10 * 1024**3

    async def test_quota_rejects_unparsable_value(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="alice", status="approved")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/users")

        response = client.post(
            "/admin/users/alice/quota",
            data={"_csrf": csrf, "max_bytes": "banana"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "quota_error=validation_failed" in response.headers["location"]
        assert await _quota_of(factory, "alice") == 10 * 1024**3
        assert await _audit_actions(factory, "admin.quota") == []

    async def test_gpu_editor_round_trip(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="alice", status="approved")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/users")

        response = client.post(
            "/admin/users/alice/gpu",
            data={"_csrf": csrf, "gpu_ids": " gpu-0,gpu-1 gpu-0 "},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/users?gpu_saved=1"
        assert json.loads(await _gpu_of(factory, "alice")) == ["gpu-0", "gpu-1"]
        rows = await _audit_actions(factory, "admin.gpu")
        assert len(rows) == 1
        assert json.loads(rows[0].detail) == ["gpu-0", "gpu-1"]

        page = client.get("/admin/users")
        assert page.status_code == 200
        assert "gpu-0" in page.text

    async def test_gpu_editor_empty_input_clears_assignment(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="alice", status="approved")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/users")
        client.post("/admin/users/alice/gpu", data={"_csrf": csrf, "gpu_ids": "gpu-9"})

        response = client.post(
            "/admin/users/alice/gpu", data={"_csrf": csrf, "gpu_ids": ""}, follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/users?gpu_saved=1"
        assert await _gpu_of(factory, "alice") is None
        rows = await _audit_actions(factory, "admin.gpu")
        assert json.loads(rows[-1].detail) == {"cleared": True}


class TestCsrfAndRollback:
    """Every POST verifies CSRF first; failed mutations roll the session back."""

    @pytest.mark.parametrize("action", ACTIONS)
    async def test_post_without_csrf_is_403(
        self,
        action: str,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        factory,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="alice", status="approved")
        _login(client, "root")
        _csrf_token(client, "/admin/users")

        response = client.post(f"/admin/users/alice/{action}", follow_redirects=False)
        assert response.status_code == 403
        assert await _status_of(factory, "alice") == "approved"
        assert await _gpu_of(factory, "alice") is None
        assert await _quota_of(factory, "alice") == 10 * 1024**3
        for action_name in ("user.ban", "user.approve", "admin.quota", "admin.gpu"):
            assert await _audit_actions(factory, action_name) == []

    async def test_approving_an_approved_user_flashes_conflict_and_writes_nothing(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="alice", status="approved")
        # A still-pending signup is what puts an Approve form on the page;
        # alice is then approved from that stale queue (two tabs open, or
        # the decision was already made elsewhere) and must conflict.
        await _seed(factory, username="stale", status="pending")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/signups")

        response = client.post(
            "/admin/users/alice/approve", data={"_csrf": csrf}, follow_redirects=False
        )
        assert response.status_code == 303
        assert "signup_error=conflict" in response.headers["location"]
        assert await _status_of(factory, "alice") == "approved"
        assert await _status_of(factory, "stale") == "pending"
        assert await _audit_actions(factory, "user.approve") == []

    async def test_unknown_action_is_404(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/users")
        response = client.post(
            "/admin/users/alice/explode", data={"_csrf": csrf}, follow_redirects=False
        )
        assert response.status_code == 404

    async def test_error_message_is_quoted_and_capped(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, factory
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        await _seed(factory, username="alice", status="approved")
        _login(client, "root")
        csrf = _csrf_token(client, "/admin/users")
        client.post(
            "/admin/users/alice/quota",
            data={"_csrf": csrf, "max_bytes": "x" * 900},
            follow_redirects=False,
        )
        location = client.post(
            "/admin/users/alice/quota",
            data={"_csrf": csrf, "max_bytes": "x" * 900},
            follow_redirects=False,
        ).headers["location"]
        assert location.startswith("/admin/users?quota_error=validation_failed&message=")
        # The 240-char cap applies to the echoed prose, not the code.
        assert len(location) < 400


class TestFlashRendering:
    """Flashes reach the page as rendered text, not raw JSON."""

    async def test_error_flash_renders_code_and_message(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        _login(client, "root")
        response = client.get(
            "/admin/users?user_error=forbidden&message=admins%20cannot%20ban%20themselves"
        )
        assert response.status_code == 200
        assert "forbidden" in response.text
        assert "admins cannot ban themselves" in response.text

    async def test_saved_flash_renders(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        _login(client, "root")
        response = client.get("/admin/users?quota_saved=1")
        assert response.status_code == 200
        assert "quota_saved" not in response.text
        assert "saved" in response.text.lower()


class TestNavAndMarkup:
    """The nav entry is role-gated; the page ships no inline JS."""

    async def test_nav_shows_admin_entry_for_admin(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        _login(client, "root")
        response = client.get("/")
        nav = response.text.split('<nav class="navbar"', 1)[1].split("</nav>", 1)[0]
        assert 'href="/admin"' in nav

    async def test_nav_hides_admin_entry_for_normal_user(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        response = client.get("/")
        nav = response.text.split('<nav class="navbar"', 1)[1].split("</nav>", 1)[0]
        assert 'href="/admin"' not in nav

    async def test_nav_hides_admin_entry_for_anonymous(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/")
        nav = response.text.split('<nav class="navbar"', 1)[1].split("</nav>", 1)[0]
        assert 'href="/admin"' not in nav

    async def test_admin_page_has_no_inline_script_or_onclick(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="root", role="admin")
        _login(client, "root")
        response = client.get("/admin/users")
        assert response.status_code == 200
        assert "<script>" not in response.text
        assert "onclick=" not in response.text
