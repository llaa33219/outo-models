"""Catalog search/filter + Space runtime tile UI contracts (v0.5.0).

Covers:

* `/models`, `/datasets`, `/spaces` filter by q/owner and sort by
  recent / downloads / likes (server-side WHERE clauses; no N+1 for
  like counts).
* Empty-state tile when filters exclude every repo.
* Shareable URLs (query string) — Clear link returns to default sort.
* Space runtime tile on the repo page (state chip + owner-only Start /
  Stop capsules). Disabled branch surfaces an admin hint instead of
  buttons. The "Open the Space" link + iframe only render when the
  runtime reports `running` and a URL.
* Owner-only POSTs for start/stop (anonymous → 303 /login, stranger →
  403, missing CSRF → 403). When `spaces_runtime_enabled` is False the
  handler just re-renders (no 503, no Podman call).
"""

from __future__ import annotations

import re
from pathlib import Path

from dulwich import porcelain
from fastapi import FastAPI
from fastapi.testclient import TestClient

from outo_models.repos.storage import repo_fs_path


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text


def _form_csrf(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, path
    cookie_token = response.cookies.get("_csrf") or client.cookies.get("_csrf")
    assert cookie_token, path
    match = re.search(r'name="_csrf" value="([^"]+)"', response.text)
    assert match is not None, path
    return cookie_token


def _seed_bare_repo_with_readme(
    tmp_data_dir: Path, *, owner: str, name: str, files: dict[str, str]
) -> None:
    work = tmp_data_dir / "src-repo"
    if work.exists():
        import shutil

        shutil.rmtree(work)
    work.mkdir()
    for rel, content in files.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    porcelain.init(str(work))
    porcelain.add(str(work), paths=list(files.keys()))
    porcelain.commit(
        str(work),
        message=b"init",
        author=b"alice <a@example.com>",
        committer=b"alice <a@example.com>",
    )
    bare = repo_fs_path(owner, name)
    bare.parent.mkdir(parents=True, exist_ok=True)
    if bare.exists():
        import shutil

        shutil.rmtree(bare)
    porcelain.clone(str(work), str(bare), bare=True)


# ---------------------------------------------------------------------------
# Catalog filter panel — server-side WHERE clauses + shareable URLs.
# ---------------------------------------------------------------------------


class TestCatalogFilterPanel:
    """/models, /datasets, /spaces all render the same filter panel."""

    async def _alice_bob_and_repos(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> TestClient:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        for body in [
            {
                "name": "alpha-model",
                "kind": "model",
                "visibility": "public",
                "description": "alpha is a foundation model for text",
            },
            {
                "name": "beta-dataset",
                "kind": "dataset",
                "visibility": "public",
                "description": "beta dataset curated for training",
            },
        ]:
            response = client.post("/api/repos", json=body)
            assert response.status_code == 201, response.text
        # Real Space under /alice for the spaces catalog filter test.
        created = client.post(
            "/api/spaces",
            json={
                "name": "demo-space",
                "sdk": "static",
                "visibility": "public",
                "description": "demo of a runnable demo space",
            },
        )
        assert created.status_code == 201, created.text
        client.post("/api/auth/logout")
        _login(client, "bob")
        for body in [
            {
                "name": "bob-model",
                "kind": "model",
                "visibility": "public",
                "description": "bob's own foundation model",
            },
            {
                "name": "bob-dataset",
                "kind": "dataset",
                "visibility": "public",
                "description": "bob data warehouse",
            },
        ]:
            response = client.post("/api/repos", json=body)
            assert response.status_code == 201, response.text
        # Bob likes alice's alpha so the like-count sort has a non-zero datum.
        liked = client.post("/api/repos/alice/alpha-model/like")
        assert liked.status_code in (200, 201), liked.text
        client.post("/api/auth/logout")
        return client

    async def test_models_filter_panel_renders_with_form(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._alice_bob_and_repos(app, seed_approved_user)
        response = client.get("/models")
        assert response.status_code == 200
        body = response.text
        for needle in (
            'action="/models"',
            'name="q"',
            'name="owner"',
            'name="sort"',
        ):
            assert needle in body, needle

    async def test_models_q_matches_name_or_description(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._alice_bob_and_repos(app, seed_approved_user)
        # Match by name.
        response = client.get("/models?q=alpha")
        assert response.status_code == 200
        assert "alice/alpha-model" in response.text
        assert "alice/demo-space" not in response.text
        assert "bob/bob-model" not in response.text
        # Match by description (lowercase substring, case-insensitive).
        response = client.get("/models?q=FOUNDATION")
        assert response.status_code == 200
        body = response.text
        assert "alice/alpha-model" in body
        assert "bob/bob-model" in body
        assert "alice/demo-space" not in body
        # No match.
        response = client.get("/models?q=zzzzzz")
        assert response.status_code == 200
        assert "alice/alpha-model" not in response.text
        assert "No models matched" in response.text

    async def test_models_owner_filter_narrows_to_owner(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._alice_bob_and_repos(app, seed_approved_user)
        response = client.get("/models?owner=alice")
        assert response.status_code == 200
        body = response.text
        assert "alice/alpha-model" in body
        assert "bob/bob-model" not in body

    async def test_models_sort_by_likes_orders_top_liked_first(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._alice_bob_and_repos(app, seed_approved_user)
        response = client.get("/models?sort=likes")
        assert response.status_code == 200
        body = response.text
        alpha_pos = body.find("alice/alpha-model")
        bob_pos = body.find("bob/bob-model")
        assert alpha_pos != -1 and bob_pos != -1
        assert alpha_pos < bob_pos

    async def test_models_sort_by_downloads_supports_zero_counts(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._alice_bob_and_repos(app, seed_approved_user)
        # downloads_count is 0 for every seeded repo — sort=downloads must
        # still render 200 and not 500.
        response = client.get("/models?sort=downloads")
        assert response.status_code == 200

    async def test_models_invalid_sort_falls_back_to_recent(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._alice_bob_and_repos(app, seed_approved_user)
        response = client.get("/models?sort=hax")
        assert response.status_code == 200
        body = response.text
        # The dropdown must show "Most recent" as the selected option,
        # not whatever the user passed.
        assert 'value="recent" selected' in body

    async def test_catalog_filter_state_shown_in_panel(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._alice_bob_and_repos(app, seed_approved_user)
        response = client.get("/models?q=alpha&owner=alice&sort=likes")
        assert response.status_code == 200
        body = response.text
        assert 'value="alpha"' in body
        assert 'value="alice"' in body
        assert 'value="likes" selected' in body
        assert "q: alpha" in body
        assert "owner: alice" in body
        assert "sort: likes" in body
        assert 'href="/models"' in body

    async def test_datasets_and_spaces_share_the_filter_panel(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._alice_bob_and_repos(app, seed_approved_user)
        for kind, expected_attr in (("datasets", "beta-dataset"), ("spaces", "demo-space")):
            response = client.get(f"/{kind}?q={expected_attr[:5]}")
            assert response.status_code == 200
            # The filter panel must point at the right URL when the user
            # clicks Apply from another catalog.
            assert f'action="/{kind}"' in response.text
            assert expected_attr in response.text

    async def test_spaces_catalog_does_not_include_models(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._alice_bob_and_repos(app, seed_approved_user)
        response = client.get("/spaces")
        assert response.status_code == 200
        body = response.text
        # Models and datasets must NOT appear under /spaces.
        assert "alice/alpha-model" not in body
        assert "alice/beta-dataset" not in body
        # But the seeded demo-space (kind=space) does.
        assert "alice/demo-space" in body

    async def test_empty_filter_result_renders_friendly_empty_state(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._alice_bob_and_repos(app, seed_approved_user)
        response = client.get("/models?q=zzzzzzzzz")
        assert response.status_code == 200
        body = response.text
        assert "No models matched" in body
        assert "Clear filters" in body


# ---------------------------------------------------------------------------
# Space runtime tile + start/stop POSTs.
# ---------------------------------------------------------------------------


class TestSpaceRuntimeTile:
    """The Space page adds a Runtime sidebar tile (state chip + owner POSTs)."""

    async def _seed_anon_space(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> TestClient:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        created = client.post(
            "/api/spaces",
            json={"name": "demo-space", "sdk": "static", "visibility": "public"},
        )
        assert created.status_code == 201, created.text
        client.post("/api/auth/logout")
        return client

    async def test_runtime_tile_renders_when_runtime_disabled(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        # Default settings: spaces_runtime_enabled=False.
        client = await self._seed_anon_space(app, seed_approved_user)
        response = client.get("/alice/demo-space")
        assert response.status_code == 200
        body = response.text
        assert "repo-sidebar__tile--runtime" in body
        # State chip shows "disabled" and the message is the admin hint.
        assert "repo-sidebar__runtime-chip--disabled" in body
        assert "disabled" in body
        # The disabled branch shows the OUTO_SPACES_RUNTIME_ENABLED hint,
        # NOT start/stop capsules.
        assert "OUTO_SPACES_RUNTIME_ENABLED" in body
        assert 'action="/alice/demo-space/space/start"' not in body
        assert 'action="/alice/demo-space/space/stop"' not in body
        assert "<iframe" not in body

    async def test_runtime_enabled_shows_owner_start_button(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        monkeypatch,
    ) -> None:
        client, app_obj, _ = app  # type: ignore[misc]
        # Flip the runtime flag on the live app + stub runtime_status.
        from copy import deepcopy

        live_settings = deepcopy(app_obj.state.settings)
        live_settings.spaces_runtime_enabled = True
        app_obj.state.settings = live_settings

        await seed_approved_user(username="alice")
        _login(client, "alice")
        created = client.post(
            "/api/spaces",
            json={"name": "demo-run", "sdk": "static", "visibility": "public"},
        )
        assert created.status_code == 201, created.text

        import outo_models.server.routers.ui as _ui_mod
        from outo_models.spaces.runtime import RuntimeState, RuntimeStatus

        class _StubAsync:
            async def __call__(self, *args, **kwargs):
                return RuntimeStatus(state=RuntimeState.STOPPED, message="stopped", url=None)

        monkeypatch.setattr(_ui_mod, "runtime_status_async", _StubAsync())
        response = client.get("/alice/demo-run")
        assert response.status_code == 200
        body = response.text
        assert "repo-sidebar__tile--runtime" in body
        assert "stopped" in body
        assert 'action="/alice/demo-run/space/start"' in body
        assert "OUTO_SPACES_RUNTIME_ENABLED" not in body
        client.post("/api/auth/logout")

    async def test_runtime_running_renders_open_link_and_iframe(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        monkeypatch,
    ) -> None:
        client, app_obj, _ = app  # type: ignore[misc]
        from copy import deepcopy

        live_settings = deepcopy(app_obj.state.settings)
        live_settings.spaces_runtime_enabled = True
        app_obj.state.settings = live_settings

        await seed_approved_user(username="alice")
        _login(client, "alice")
        created = client.post(
            "/api/spaces",
            json={"name": "running-demo", "sdk": "static", "visibility": "public"},
        )
        assert created.status_code == 201, created.text

        import outo_models.server.routers.ui as _ui_mod
        from outo_models.spaces.runtime import RuntimeState, RuntimeStatus

        class _RunningStub:
            async def __call__(self, *args, **kwargs):
                return RuntimeStatus(
                    state=RuntimeState.RUNNING,
                    message="running",
                    url="http://localhost/spaces/alice/running-demo/run/",
                )

        monkeypatch.setattr(_ui_mod, "runtime_status_async", _RunningStub())
        response = client.get("/alice/running-demo")
        assert response.status_code == 200
        body = response.text
        assert "repo-sidebar__runtime-chip--running" in body
        assert "running" in body
        assert "Open the Space" in body
        assert "running-demo/run/" in body
        assert "<iframe" in body
        assert 'action="/alice/running-demo/space/stop"' in body
        client.post("/api/auth/logout")

    async def test_start_post_anonymous_redirects_to_login(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/spaces",
            json={"name": "anon-space", "sdk": "static", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        response = client.get("/alice/anon-space")
        csrf = response.cookies.get("_csrf") or client.cookies.get("_csrf")
        assert csrf
        posted = client.post(
            "/alice/anon-space/space/start",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        assert posted.headers["location"].startswith("/login")

    async def test_start_post_stranger_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/spaces",
            json={"name": "private-space", "sdk": "static", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")
        csrf = _form_csrf(client, "/alice/private-space")
        posted = client.post(
            "/alice/private-space/space/start",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert posted.status_code == 403

    async def test_start_post_without_csrf_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        client.post(
            "/api/spaces",
            json={"name": "nocsrf-space", "sdk": "static", "visibility": "public"},
        )
        posted = client.post(
            "/alice/nocsrf-space/space/start",
            data={},
            follow_redirects=False,
        )
        assert posted.status_code == 403

    async def test_stop_post_stranger_returns_403(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        await seed_approved_user(username="bob")
        _login(client, "alice")
        client.post(
            "/api/spaces",
            json={"name": "stopx-space", "sdk": "static", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        _login(client, "bob")
        csrf = _form_csrf(client, "/alice/stopx-space")
        posted = client.post(
            "/alice/stopx-space/space/stop",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert posted.status_code == 403
