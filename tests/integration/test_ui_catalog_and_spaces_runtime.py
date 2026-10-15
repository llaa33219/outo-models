"""Catalog search/filter + Space runtime tile UI contracts (v0.5.0).

Covers:

* `/models`, `/datasets`, `/spaces` filter by q/owner and sort by
  recent / downloads / likes (server-side WHERE clauses; no N+1 for
  like counts).
* Empty-state tile when filters exclude every repo.
* Shareable URLs (query string) — Clear link returns to default sort.
* Space Runtime tab — the landing view for spaces (HF Spaces
  parity): `/{owner}/{name}` and `/{owner}/{name}/runtime` render it,
  `/{owner}/{name}/card` holds the card, and the tab strip leads with
  Runtime. The tab carries the status chip, the primary Start/Stop
  control, the full-area app iframe (path-relative `src`) or one large
  state panel — stopped / failed / building / runtime-disabled.
* Owner-only POSTs for start/stop (anonymous → 303 /login, stranger →
  403, missing CSRF → 403). Success redirects carry `?started=1` /
  `?stopped=1`; a domain failure carries `?runtime_error=<code>` and
  an unexpected exception is logged and reported as `internal_error`.
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


def _tab_labels(body: str) -> list[str]:
    """Label of every tab link, in document order."""
    return re.findall(r'class="tabs__link[^"]*">([^<]+)</a>', body)


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
# Space Runtime tab — the Space landing view (HF Spaces parity).
#
# URL mapping pinned here: a space's bare URL is the Runtime tab, the
# card tab moved to `/{owner}/{name}/card`, and `/runtime` is the
# explicit alias of the bare URL (spaces only).
# ---------------------------------------------------------------------------


class TestSpaceRuntimeTab:
    """Runtime is the default Space tab; models/datasets are untouched."""

    async def _seed_space(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
        *,
        name: str,
        sdk: str = "static",
        logout: bool = True,
    ) -> TestClient:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        created = client.post(
            "/api/spaces",
            json={"name": name, "sdk": sdk, "visibility": "public"},
        )
        assert created.status_code == 201, created.text
        if logout:
            client.post("/api/auth/logout")
        return client

    async def test_static_space_without_podman_renders_stopped_state(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        """Frozen contract: `static` is answered from the exported site
        dir, never from Podman. No site dir -> `stopped` (never
        `failed`), so a missing Podman socket cannot produce an error
        panel for a static Space.
        """
        client = await self._seed_space(app, seed_approved_user, name="demo-space")
        response = client.get("/alice/demo-space")
        assert response.status_code == 200
        body = response.text
        assert 'class="space-runtime__chip space-runtime__chip--stopped"' in body
        assert 'class="space-runtime__panel space-runtime__panel--stopped"' in body
        assert "This Space is currently stopped" in body
        assert "<iframe" not in body
        assert "Open the Space" not in body
        # The Runtime tab owns the runtime state — no sidebar tile, and
        # the metadata sidebar is collapsed out of the way.
        assert '<section class="repo-sidebar__tile repo-sidebar__tile--runtime"' not in body
        assert 'class="repo-layout repo-layout--runtime"' in body

    async def test_container_space_with_unreachable_podman_renders_failed_state(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        """Frozen contract: a non-static Space with no reachable Podman
        reports `failed` with the runtime-manager message, and the
        Runtime tab turns that into the big error panel.
        """
        client = await self._seed_space(app, seed_approved_user, name="podman-space", sdk="docker")
        response = client.get("/alice/podman-space")
        assert response.status_code == 200
        body = response.text
        assert 'class="space-runtime__chip space-runtime__chip--failed"' in body
        assert 'class="space-runtime__panel space-runtime__panel--error"' in body
        assert "This Space could not start" in body
        assert 'role="alert"' in body
        assert "error code: runtime_failed" in body
        assert "<iframe" not in body

    async def test_runtime_error_query_param_renders_the_error_panel(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        """A failed Start/Stop POST redirects back with
        `?runtime_error=<code>&message=<text>`; the panel shows both
        even when the runtime status itself reads `stopped`.
        """
        client = await self._seed_space(app, seed_approved_user, name="queryerr-space")
        response = client.get(
            "/alice/queryerr-space",
            params={
                "runtime_error": "space_start_failed",
                "message": "podman socket /run/podman/podman.sock is missing",
            },
        )
        assert response.status_code == 200
        body = response.text
        assert 'class="space-runtime__panel space-runtime__panel--error"' in body
        assert "error code: space_start_failed" in body
        assert "podman socket" in body
        # The form outcome replaces the stopped panel, it does not
        # stack a second Start button next to it.
        assert 'class="space-runtime__panel space-runtime__panel--stopped"' not in body

    async def test_bare_space_url_is_the_runtime_tab_and_card_moves_to_card_url(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._seed_space(app, seed_approved_user, name="landing-space")

        bare = client.get("/alice/landing-space")
        assert bare.status_code == 200
        bare_body = bare.text
        assert '<main class="main--space-runtime">' in bare_body
        assert 'class="space-runtime__bar"' in bare_body
        assert 'tabs__link--active">Runtime</a>' in bare_body

        alias = client.get("/alice/landing-space/runtime")
        assert alias.status_code == 200
        assert '<main class="main--space-runtime">' in alias.text

        card = client.get("/alice/landing-space/card")
        assert card.status_code == 200
        card_body = card.text
        assert 'class="space-runtime__bar"' not in card_body
        assert '<main class="main--space-runtime">' not in card_body
        assert 'class="tabs__link tabs__link--active">Space card</a>' in card_body

    async def test_space_tab_order_leads_with_runtime_and_model_tab_order_is_unchanged(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        assert (
            client.post(
                "/api/spaces",
                json={"name": "tab-order-space", "sdk": "static", "visibility": "public"},
            ).status_code
            == 201
        )
        assert (
            client.post(
                "/api/repos",
                json={"name": "tab-order-model", "kind": "model", "visibility": "public"},
            ).status_code
            == 201
        )
        client.post("/api/auth/logout")

        space = client.get("/alice/tab-order-space/card").text
        model = client.get("/alice/tab-order-model").text

        assert [label for label in _tab_labels(space)] == [
            "Runtime",
            "Space card",
            "Files",
            "Community",
        ]
        # Non-space repos keep the four original tabs in the original
        # order and the bare URL as their card tab.
        assert [label for label in _tab_labels(model)] == [
            "Model card",
            "Files",
            "Community",
        ]
        assert 'href="/alice/tab-order-model" class="tabs__link tabs__link--active"' in model

    async def test_runtime_url_is_404_for_models_and_datasets(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        for name, kind in (("rt-model", "model"), ("rt-dataset", "dataset")):
            assert (
                client.post(
                    "/api/repos",
                    json={"name": name, "kind": kind, "visibility": "public"},
                ).status_code
                == 201
            )
        client.post("/api/auth/logout")
        for name in ("rt-model", "rt-dataset"):
            assert client.get(f"/alice/{name}/runtime").status_code == 404
            # ... while the bare URL still renders the card tab.
            assert client.get(f"/alice/{name}").status_code == 200

    async def test_running_space_renders_full_area_iframe_with_relative_src(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, monkeypatch
    ) -> None:
        client = await self._seed_space(app, seed_approved_user, name="running-demo", logout=False)

        import outo_models.server.routers.ui as _ui_mod
        from outo_models.spaces.runtime import RuntimeState, RuntimeStatus

        class _RunningStub:
            async def __call__(self, *args, **kwargs):
                return RuntimeStatus(
                    state=RuntimeState.RUNNING,
                    message="running",
                    url="https://models.example.com/spaces/alice/running-demo/run/",
                )

        monkeypatch.setattr(_ui_mod, "runtime_status_async", _RunningStub())
        response = client.get("/alice/running-demo")
        assert response.status_code == 200
        body = response.text
        assert 'class="space-runtime__chip space-runtime__chip--running"' in body
        assert 'class="space-runtime__frame"' in body
        # The iframe MUST take the path-relative run URL; the absolute
        # `url` only backs the new-tab link (reverse-proxy safe).
        assert 'src="/spaces/alice/running-demo/run/"' in body
        assert 'loading="eager"' in body
        assert 'referrerpolicy="no-referrer"' in body
        assert "https://models.example.com/spaces/alice/running-demo/run/" in body
        assert "Open the Space" in body
        assert 'action="/alice/running-demo/space/stop"' in body
        assert 'action="/alice/running-demo/space/start"' not in body
        client.post("/api/auth/logout")

    async def test_owner_sees_the_big_start_button_on_the_stopped_panel(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._seed_space(app, seed_approved_user, name="demo-run", logout=False)
        response = client.get("/alice/demo-run")
        assert response.status_code == 200
        body = response.text
        # Big CTA in the stopped panel AND the slim header strip.
        assert 'class="space-runtime__btn space-runtime__btn--start space-runtime__btn--lg"' in body
        assert 'class="space-runtime__btn space-runtime__btn--start"' in body
        assert 'action="/alice/demo-run/space/start"' in body
        assert "OUTO_SPACES_RUNTIME_ENABLED" not in body
        client.post("/api/auth/logout")

    async def test_anonymous_viewer_gets_no_start_action(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._seed_space(app, seed_approved_user, name="anon-view-space")
        body = client.get("/alice/anon-view-space").text
        assert 'class="space-runtime__panel space-runtime__panel--stopped"' in body
        assert 'action="/alice/anon-view-space/space/start"' not in body
        assert 'action="/alice/anon-view-space/space/stop"' not in body
        # Settings stays owner/admin-only, Runtime stays public.
        assert ">Settings</a>" not in body

    async def test_runtime_disabled_renders_the_operator_notice(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client, app_obj, _ = app  # type: ignore[misc]
        from copy import deepcopy

        live_settings = deepcopy(app_obj.state.settings)
        live_settings.spaces_runtime_enabled = False
        app_obj.state.settings = live_settings

        client = await self._seed_space(app, seed_approved_user, name="disabled-space")
        response = client.get("/alice/disabled-space")
        assert response.status_code == 200
        body = response.text
        assert 'class="space-runtime__panel space-runtime__panel--notice"' in body
        assert "OUTO_SPACES_RUNTIME_ENABLED" in body
        assert 'class="space-runtime__chip space-runtime__chip--disabled"' in body
        assert 'action="/alice/disabled-space/space/start"' not in body
        assert 'action="/alice/disabled-space/space/stop"' not in body
        assert "<iframe" not in body

    async def test_sidebar_runtime_tile_stays_on_the_other_tabs_without_the_iframe(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user
    ) -> None:
        client = await self._seed_space(app, seed_approved_user, name="tile-space")
        body = client.get("/alice/tile-space/card").text
        assert '<section class="repo-sidebar__tile repo-sidebar__tile--runtime"' in body
        assert 'class="repo-sidebar__runtime-chip repo-sidebar__runtime-chip--stopped"' in body
        # The 220px sidebar preview is gone; the app lives on Runtime.
        assert "<iframe" not in body


class TestSpaceRuntimeFormOutcomes:
    """Start/Stop POSTs report the outcome on the redirect query."""

    async def _owner_with_space(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, name: str
    ) -> TestClient:
        client, _, _ = app
        await seed_approved_user(username="alice")
        _login(client, "alice")
        assert (
            client.post(
                "/api/spaces",
                json={"name": name, "sdk": "static", "visibility": "public"},
            ).status_code
            == 201
        )
        return client

    async def test_success_redirect_carries_started(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, monkeypatch
    ) -> None:
        client = await self._owner_with_space(app, seed_approved_user, "ok-start-space")
        import outo_models.server.routers.spaces as _spaces_mod

        async def _ok(**kwargs):
            return None

        monkeypatch.setattr(_spaces_mod, "_run_lifecycle", _ok)
        csrf = _form_csrf(client, "/alice/ok-start-space")
        posted = client.post(
            "/alice/ok-start-space/space/start",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        assert posted.headers["location"] == "/alice/ok-start-space?started=1"

    async def test_success_redirect_carries_stopped(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, monkeypatch
    ) -> None:
        client = await self._owner_with_space(app, seed_approved_user, "ok-stop-space")
        import outo_models.server.routers.spaces as _spaces_mod

        async def _ok(**kwargs):
            return None

        monkeypatch.setattr(_spaces_mod, "_run_lifecycle", _ok)
        csrf = _form_csrf(client, "/alice/ok-stop-space")
        posted = client.post(
            "/alice/ok-stop-space/space/stop",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        assert posted.headers["location"] == "/alice/ok-stop-space?stopped=1"

    async def test_domain_failure_redirect_carries_the_error_code(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, monkeypatch
    ) -> None:
        client = await self._owner_with_space(app, seed_approved_user, "boom-space")
        import outo_models.server.routers.spaces as _spaces_mod
        from outo_models.exceptions import OutoError

        async def _boom(**kwargs):
            raise OutoError("podman refused to start the container", code="space_start_failed")

        monkeypatch.setattr(_spaces_mod, "_run_lifecycle", _boom)
        csrf = _form_csrf(client, "/alice/boom-space")
        posted = client.post(
            "/alice/boom-space/space/start",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        location = posted.headers["location"]
        assert location.startswith("/alice/boom-space?runtime_error=space_start_failed")
        assert "message=podman%20refused%20to%20start%20the%20container" in location

    async def test_unexpected_failure_redirects_as_internal_error(
        self, app: tuple[TestClient, FastAPI, object], seed_approved_user, monkeypatch
    ) -> None:
        client = await self._owner_with_space(app, seed_approved_user, "crash-space")
        import outo_models.server.routers.spaces as _spaces_mod

        async def _boom(**kwargs):
            raise ValueError("secret token sk-abcdef must not leak")

        monkeypatch.setattr(_spaces_mod, "_run_lifecycle", _boom)
        csrf = _form_csrf(client, "/alice/crash-space")
        posted = client.post(
            "/alice/crash-space/space/start",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert posted.status_code == 303
        location = posted.headers["location"]
        assert "runtime_error=internal_error" in location
        assert "secret" not in location
        assert "ValueError" not in location

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
        # A stranger gets no Start form on the Runtime tab, so the CSRF
        # pair is minted from a page that does render one for them.
        csrf = _form_csrf(client, "/alice/private-space/card")
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
        csrf = _form_csrf(client, "/alice/stopx-space/card")
        posted = client.post(
            "/alice/stopx-space/space/stop",
            data={"_csrf": csrf},
            follow_redirects=False,
        )
        assert posted.status_code == 403
