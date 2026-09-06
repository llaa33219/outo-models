"""Recursive tree walk over the server's `/files` endpoint."""

from __future__ import annotations

from typing import Any

import httpx

from outo_models_cli import api
from outo_models_cli.errors import OmcError, map_response_error, map_transport_error
from outo_models_cli.matchers import passes


async def list_files_async(
    client: httpx.AsyncClient,
    *,
    owner: str,
    name: str,
    path: str,
    revision: str,
) -> list[api.FileEntry]:
    """Async equivalent of `api.list_files` (uses the shared async client)."""
    try:
        response = await client.get(
            f"/api/repos/{owner}/{name}/files",
            params={
                "path": path,
                **({"revision": revision} if revision else {}),
            },
        )
    except httpx.HTTPError as exc:
        raise map_transport_error(exc) from exc
    if response.status_code >= 400:
        raise map_response_error(response)
    payload: Any = response.json()
    rows = payload.get("entries", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        raise OmcError("`/files` returned a malformed `entries` field.")
    entries: list[api.FileEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        size = row.get("size_bytes")
        entries.append(
            api.FileEntry(
                name=str(row.get("name", "")),
                path=str(row.get("path", "")),
                kind=str(row.get("kind", "")),
                size_bytes=int(size) if isinstance(size, (int, float)) else None,
            )
        )
    return entries


async def walk_repo_async(
    client: httpx.AsyncClient,
    *,
    owner: str,
    name: str,
    revision: str,
    include: list[str],
    exclude: list[str],
) -> list[api.FileEntry]:
    """Recursively enumerate every file under a repo (depth-first)."""
    out: list[api.FileEntry] = []

    async def visit(directory: str) -> None:
        rows = await list_files_async(
            client,
            owner=owner,
            name=name,
            path=directory,
            revision=revision,
        )
        for entry in rows:
            if entry.kind == "dir":
                await visit(entry.path)
            elif entry.kind == "file" and passes(
                entry.path,
                include=include,
                exclude=exclude,
            ):
                out.append(entry)

    await visit("")
    return out


__all__ = ["list_files_async", "walk_repo_async"]
