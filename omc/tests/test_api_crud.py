"""Tests for the typed API client: auth + repos CRUD + files listing."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from outo_models_cli import api
from outo_models_cli.errors import (
    AuthInvalidError,
    ForbiddenError,
    NotFoundError,
    ServerUnreachableError,
    ValidationFailedError,
)

SERVER = "http://api.test"
TOKEN = "pat-abc"


@pytest.fixture
def client() -> httpx.Client:
    """An `httpx.Client` pointed at the test server with respx mocked."""
    return api.with_client(SERVER, TOKEN)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_me_returns_who_am_i(client: httpx.Client) -> None:
    respx.get(f"{SERVER}/api/auth/me").respond(
        200,
        json={"username": "alice", "role": "user"},
    )
    with client:
        result = api.me(client)
    assert result.username == "alice"
    assert result.role == "user"
    assert result.server == SERVER


def test_me_invalid_token_raises(client: httpx.Client) -> None:
    respx.get(f"{SERVER}/api/auth/me").respond(401, json={"detail": "expired"})
    with client, pytest.raises(AuthInvalidError) as exc:
        api.me(client)
    assert "expired" in str(exc.value)


# ---------------------------------------------------------------------------
# Repos CRUD
# ---------------------------------------------------------------------------


def _summary_payload(**overrides: Any) -> dict[str, Any]:
    """Default `/api/repos` row, overridable per test."""
    base: dict[str, Any] = {
        "name": "bert",
        "kind": "model",
        "visibility": "private",
        "description": "A test repo",
        "size_bytes": 1024,
        "owner": "alice",
        "clone_url": "https://api.test/alice/bert.git",
    }
    base.update(overrides)
    return base


def test_create_repo_returns_summary(client: httpx.Client) -> None:
    route = respx.post(f"{SERVER}/api/repos").respond(201, json=_summary_payload())
    with client:
        result = api.create_repo(
            client,
            name="bert",
            kind="model",
            visibility="private",
            description=None,
        )
    assert result.name == "bert"
    assert route.called


def test_create_repo_validation_failed(client: httpx.Client) -> None:
    respx.post(f"{SERVER}/api/repos").respond(422, json={"detail": "bad name"})
    with client, pytest.raises(ValidationFailedError):
        api.create_repo(
            client,
            name="bad",
            kind="model",
            visibility="private",
            description=None,
        )


def test_delete_repo_sends_kind_param(client: httpx.Client) -> None:
    route = respx.delete(
        f"{SERVER}/api/repos/alice/bert",
        params={"kind": "model"},
    ).respond(204)
    with client:
        api.delete_repo(client, owner="alice", name="bert", kind="model")
    assert route.called


def test_delete_repo_forbidden(client: httpx.Client) -> None:
    respx.delete(f"{SERVER}/api/repos/alice/bert").respond(403)
    with client, pytest.raises(ForbiddenError):
        api.delete_repo(client, owner="alice", name="bert", kind="model")


def test_list_repos_returns_summaries(client: httpx.Client) -> None:
    respx.get(f"{SERVER}/api/repos").respond(
        200,
        json=[_summary_payload(name="a"), _summary_payload(name="b", owner="alice")],
    )
    with client:
        rows = api.list_repos(client)
    assert [r.name for r in rows] == ["a", "b"]


def test_list_repos_with_filters(client: httpx.Client) -> None:
    route = respx.get(
        f"{SERVER}/api/repos",
        params={"kind": "dataset", "owner": "alice"},
    ).respond(200, json=[])
    with client:
        api.list_repos(client, kind="dataset", owner="alice")
    assert route.called


def test_get_repo_returns_detail(client: httpx.Client) -> None:
    respx.get(f"{SERVER}/api/repos/alice/bert").respond(
        200,
        json=_summary_payload(downloads_count=42),
    )
    with client:
        detail = api.get_repo(client, owner="alice", name="bert")
    assert detail.downloads_count == 42


# ---------------------------------------------------------------------------
# Files / tree
# ---------------------------------------------------------------------------


def test_list_files_parses_entries(client: httpx.Client) -> None:
    respx.get(f"{SERVER}/api/repos/alice/bert/files").respond(
        200,
        json={
            "path": "",
            "entries": [
                {"name": "README.md", "path": "README.md", "kind": "file", "size_bytes": 100},
                {"name": "src", "path": "src", "kind": "dir", "size_bytes": None},
            ],
        },
    )
    with client:
        rows = api.list_files(client, owner="alice", name="bert")
    assert len(rows) == 2
    assert rows[0].name == "README.md"
    assert rows[0].size_bytes == 100
    assert rows[1].kind == "dir"


def test_list_files_forwards_revision(client: httpx.Client) -> None:
    """The CLI always sends `?revision=` — server may ignore but must not 400."""
    route = respx.get(
        f"{SERVER}/api/repos/alice/bert/files",
        params={"revision": "dev"},
    ).respond(200, json={"path": "", "entries": []})
    with client:
        api.list_files(client, owner="alice", name="bert", revision="dev")
    assert route.called


def test_list_files_not_found(client: httpx.Client) -> None:
    respx.get(f"{SERVER}/api/repos/alice/nope/files").respond(404)
    with client, pytest.raises(NotFoundError):
        api.list_files(client, owner="alice", name="nope")


# ---------------------------------------------------------------------------
# Transport-level failures
# ---------------------------------------------------------------------------


def test_server_unreachable_maps_to_clean_error(client: httpx.Client) -> None:
    respx.get(f"{SERVER}/api/auth/me").mock(side_effect=httpx.ConnectError("nope"))
    with client, pytest.raises(ServerUnreachableError):
        api.me(client)


# ---------------------------------------------------------------------------
# resolve_url — encoded correctly
# ---------------------------------------------------------------------------


def test_resolve_url_encodes_spaces() -> None:
    assert "%20" in api.resolve_url(
        owner="alice",
        name="bert",
        revision="main",
        path="dir with space/a.bin",
    )


def test_resolve_url_keeps_slashes_in_path() -> None:
    """In-repo directories must remain as `/` segments."""
    out = api.resolve_url(owner="alice", name="bert", revision="main", path="a/b/c.bin")
    assert out == "/alice/bert/resolve/main/a/b/c.bin"
