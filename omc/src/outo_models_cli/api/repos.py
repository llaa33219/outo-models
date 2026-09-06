"""Repo CRUD + tree listing over the public REST API."""

from __future__ import annotations

from typing import Any

import httpx

from outo_models_cli.api import (
    FileEntry,
    RepoDetail,
    RepoSummary,
    send,
    summary_from,
    unwrap,
)
from outo_models_cli.errors import BadResponseError, map_response_error


def create_repo(
    client: httpx.Client,
    *,
    name: str,
    kind: str,
    visibility: str,
    description: str | None,
) -> RepoSummary:
    """`POST /api/repos` — returns the new repo summary."""
    body: dict[str, Any] = {
        "name": name,
        "kind": kind,
        "visibility": visibility,
        "description": description,
    }
    response = send(client, "POST", "/api/repos", json=body)
    if response.status_code >= 400:
        raise map_response_error(response)
    return summary_from(unwrap(response))


def delete_repo(client: httpx.Client, *, owner: str, name: str, kind: str) -> None:
    """`DELETE /api/repos/{owner}/{name}?kind=` — no body."""
    response = send(client, "DELETE", f"/api/repos/{owner}/{name}", params={"kind": kind})
    if response.status_code >= 400:
        raise map_response_error(response)


def list_repos(
    client: httpx.Client,
    *,
    kind: str | None = None,
    owner: str | None = None,
) -> list[RepoSummary]:
    """`GET /api/repos` — paginated server-side; client returns the full list."""
    params: dict[str, str] = {}
    if kind:
        params["kind"] = kind
    if owner:
        params["owner"] = owner
    response = send(client, "GET", "/api/repos", params=params)
    if response.status_code >= 400:
        raise map_response_error(response)
    try:
        rows: Any = response.json()
    except ValueError as exc:
        raise BadResponseError("`/api/repos` returned a non-JSON response.") from exc
    if not isinstance(rows, list):
        raise BadResponseError("`/api/repos` returned a non-list response.")
    return [summary_from(item) for item in rows if isinstance(item, dict)]


def get_repo(client: httpx.Client, *, owner: str, name: str) -> RepoDetail:
    """`GET /api/repos/{owner}/{name}` — full detail payload."""
    response = send(client, "GET", f"/api/repos/{owner}/{name}")
    if response.status_code >= 400:
        raise map_response_error(response)
    payload = unwrap(response)
    summary = summary_from(payload)
    return RepoDetail(
        name=summary.name,
        kind=summary.kind,
        visibility=summary.visibility,
        description=summary.description,
        size_bytes=summary.size_bytes,
        owner=summary.owner,
        clone_url=summary.clone_url,
        downloads_count=int(payload.get("downloads_count", 0)),
    )


def list_files(
    client: httpx.Client,
    *,
    owner: str,
    name: str,
    path: str = "",
    revision: str | None = None,
) -> list[FileEntry]:
    """`GET /api/repos/{owner}/{name}/files?path=&revision=`.

    The server may ignore the `revision` parameter (the current
    implementation only lists the default branch tip). When it does
    the response is still well-formed; this client simply forwards the
    flag verbatim so a future revision-aware server picks it up without
    a CLI release.
    """
    params: dict[str, str] = {}
    if path:
        params["path"] = path
    if revision:
        params["revision"] = revision
    response = send(
        client,
        "GET",
        f"/api/repos/{owner}/{name}/files",
        params=params,
    )
    if response.status_code >= 400:
        raise map_response_error(response)
    payload = unwrap(response)
    rows = payload.get("entries", [])
    if not isinstance(rows, list):
        raise BadResponseError("`/files` returned a malformed `entries` field.")
    entries: list[FileEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        size = row.get("size_bytes")
        entries.append(
            FileEntry(
                name=str(row.get("name", "")),
                path=str(row.get("path", "")),
                kind=str(row.get("kind", "")),
                size_bytes=int(size) if isinstance(size, (int, float)) else None,
            )
        )
    return entries


def walk_repo(
    client: httpx.Client,
    *,
    owner: str,
    name: str,
    revision: str | None = None,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> list[FileEntry]:
    """Recursively enumerate every file under a repo."""
    from outo_models_cli.matchers import passes

    out: list[FileEntry] = []

    def visit(directory: str) -> None:
        rows = list_files(client, owner=owner, name=name, path=directory, revision=revision)
        for entry in rows:
            if entry.kind == "dir":
                visit(entry.path)
            elif entry.kind == "file" and passes(entry.path, include=include, exclude=exclude):
                out.append(entry)

    visit("")
    return out


def resolve_url(*, owner: str, name: str, revision: str, path: str) -> str:
    """Build the path-component-only URL for a raw file resolution.

    Returned as a string (no client needed) so the download command can
    hand it to its own `httpx.AsyncClient` and stream the body directly.
    """
    from urllib.parse import quote

    encoded_owner = quote(owner, safe="")
    encoded_name = quote(name, safe="")
    encoded_revision = quote(revision, safe="")
    encoded_path = quote(path, safe="/")
    return f"/{encoded_owner}/{encoded_name}/resolve/{encoded_revision}/{encoded_path}"


__all__ = [
    "create_repo",
    "delete_repo",
    "get_repo",
    "list_files",
    "list_repos",
    "resolve_url",
    "walk_repo",
]
