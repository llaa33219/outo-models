"""`omc upload <owner>/<name> <local-path>` — file or folder upload.

The command partitions the user's file set into two regimes:

* Files at or below `_MAX_FILE_BYTES` (100 MiB) ride the existing
  multipart endpoint unchanged.
* Files above the cap are routed through the Git LFS protocol: a
  single batch POST, one streaming PUT per object, and a pointer-text
  entry that gets committed through the same multipart endpoint.

The two regimes produce ONE final commit — pointers for the LFS files
ride along in the same multipart call as the small files. The
`_SINGLE_REQUEST_TOTAL_BYTES` threshold still controls whether the
small-file set is split into multiple commits, exactly as before; the
LFS path adds its own progress reporting at a higher granularity.

For the LFS path we need the username as well as the PAT (HTTP Basic
auth on the LFS surface). `resolve_username` fetches it via `/api/auth/me`
on first use and persists it in the config store so subsequent uploads
do not pay the round-trip.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import httpx
import typer

from outo_models_cli import api
from outo_models_cli.api import lfs as lfs_api
from outo_models_cli.commands._shared import (
    _MAX_FILE_BYTES,
    _SINGLE_REQUEST_TOTAL_BYTES,
    _collect_files,
    _errprint,
    _human_bytes,
    _parse_owner_repo,
    console,
    resolve_username,
)
from outo_models_cli.commands._upload_commit import _path_in_repo_for, commit_mixed
from outo_models_cli.errors import (
    AuthRequiredError,
    BadResponseError,
    OmcError,
)
from outo_models_cli.http import build_basic_client

if TYPE_CHECKING:
    from outo_models_cli.config import Store


def upload_command(
    ctx: typer.Context,
    repo: Annotated[str, typer.Argument(help="Repository as `<owner>/<name>`.")],
    local_path: Annotated[
        Path,
        typer.Argument(help="Local file or directory to upload."),
    ],
    path_in_repo: Annotated[
        str,
        typer.Option(help="Subdirectory inside the repo (default: root)."),
    ] = "",
    message: Annotated[
        str | None,
        typer.Option(help="Commit message."),
    ] = None,
    server: Annotated[str | None, typer.Option(help="Target server URL.")] = None,
) -> None:
    """Upload a file or folder to a repository."""
    store: Store = ctx.obj["store"]
    config_path: Path | None = ctx.obj.get("config_path")
    try:
        owner, name = _parse_owner_repo(repo)
        files, file_root = _collect_files(local_path)
        base_url, token = store.resolve(server)
        username, store = resolve_username(
            store,
            base_url=base_url,
            token=token,
            config_path=config_path,
        )
    except (OmcError, AuthRequiredError) as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    bearer_client = api.with_client(base_url, token)
    basic_client = build_basic_client(base_url, username, token)
    try:
        partition = lfs_api.partition_files(files, cap_bytes=_MAX_FILE_BYTES)
        if not partition.large:
            _upload_small_only(
                bearer_client,
                owner=owner,
                name=name,
                partition=partition,
                file_root=file_root,
                path_in_repo=path_in_repo,
                message=message,
            )
            return
        with basic_client:
            _run_lfs_upload(
                basic_client,
                owner=owner,
                name=name,
                partition=partition,
            )
        with bearer_client:
            result = commit_mixed(
                bearer_client,
                owner=owner,
                name=name,
                partition=partition,
                file_root=file_root,
                path_in_repo=path_in_repo,
                message=message,
            )
        console.print(
            f"Uploaded {len(result.files)} file(s) as commit {result.commit_sha}.",
        )
    except OmcError as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc
    finally:
        bearer_client.close()
        basic_client.close()


def _upload_small_only(
    client: httpx.Client,
    *,
    owner: str,
    name: str,
    partition: lfs_api.Partition,
    file_root: Path,
    path_in_repo: str,
    message: str | None,
) -> None:
    """All-small-files path: same behaviour as the pre-LFS upload command."""
    files = list(partition.small)
    total = sum(f.stat().st_size for f in files)
    if total < _SINGLE_REQUEST_TOTAL_BYTES:
        result = api.upload(
            client,
            owner=owner,
            name=name,
            files=files,
            path_in_repo=path_in_repo,
            message=message,
            file_root=file_root,
        )
        console.print(
            f"Uploaded {len(result.files)} file(s) as commit {result.commit_sha}.",
        )
        return

    last_sha = ""
    for f in files:
        sub = _path_in_repo_for(f, root=file_root, default=path_in_repo)
        single = api.upload(
            client,
            owner=owner,
            name=name,
            files=[f],
            path_in_repo=sub,
            message=message,
            file_root=file_root,
        )
        last_sha = single.commit_sha
    console.print(
        f"Uploaded {len(files)} file(s) in sequential commits; last commit {last_sha}.",
    )


def _run_lfs_upload(
    basic_client: httpx.Client,
    *,
    owner: str,
    name: str,
    partition: lfs_api.Partition,
) -> dict[str, bytes]:
    """Drive the LFS batch + PUT dance; surface per-object errors with one-line output.

    Returns the `{oid: pointer_text}` map; the caller commits it via
    `commit_mixed`. Per-object failures are printed one-per-line so the
    user sees WHICH file failed; the function then raises
    `BadResponseError` so the upload aborts (one bad object must not
    silently half-commit).
    """
    if not partition.large:
        return {}
    batch_entries, _maps = lfs_api.dedupe_objects(partition.large)
    actions, errors, present = api.batch_upload(
        basic_client,
        owner=owner,
        name=name,
        objects=batch_entries,
    )
    if errors:
        for err in errors:
            matched = next(
                (item.path for item in partition.large if item.oid == err.oid),
                None,
            )
            label = matched.name if matched is not None else err.oid[:12]
            _errprint(f"LFS upload failed for {label}: {err.code} {err.message}")
        raise BadResponseError(
            f"LFS batch reported {len(errors)} object error(s); aborting.",
        )
    if present:
        console.print(
            f"LFS: {len(present)} object(s) already on the server; skipping PUT.",
        )
    if actions:
        paths_by_oid: dict[str, list[Path]] = {}
        for item in partition.large:
            paths_by_oid.setdefault(item.oid, []).append(item.path)
        total = sum(a.size for a in actions)
        console.print(
            f"LFS: streaming {len(actions)} object(s) ({_human_bytes(total)}).",
        )
        api.upload_objects(
            basic_client,
            actions=actions,
            paths_by_oid=paths_by_oid,
        )
    return lfs_api.pointers_for(partition.large)


__all__ = ["upload_command"]
