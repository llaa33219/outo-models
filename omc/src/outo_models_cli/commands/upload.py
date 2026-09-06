"""`omc upload <owner>/<name> <local-path>` — file or folder upload."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from outo_models_cli import api
from outo_models_cli.commands._shared import (
    _SINGLE_REQUEST_TOTAL_BYTES,
    _check_size_limit,
    _collect_files,
    _errprint,
    _parse_owner_repo,
    _resolve_target,
    console,
)
from outo_models_cli.errors import (
    AuthRequiredError,
    OmcError,
)

if TYPE_CHECKING:
    from outo_models_cli.config import Store


def _path_in_repo_for(file: Path, *, root: Path, default: str) -> str:
    """Compute the destination subdirectory for one file in a folder upload.

    The server applies `path` to every filename in the multipart batch,
    so when the user uploads a folder of files we want the *common
    parent directory* (not the file's full relative path) to land under
    `path_in_repo`. Files at the root go straight under the user's
    `path_in_repo` verbatim.
    """
    try:
        rel_parent = file.resolve().relative_to(root.resolve()).parent
    except ValueError:
        return default
    if rel_parent == Path("."):
        return default
    base = default.rstrip("/")
    return f"{base}/{rel_parent.as_posix()}" if base else rel_parent.as_posix()


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
    try:
        owner, name = _parse_owner_repo(repo)
        files, file_root = _collect_files(local_path)
        _check_size_limit(files)
        _base_url, client = _resolve_target(store, server=server)
    except (OmcError, AuthRequiredError) as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc

    try:
        with client:
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

            # Large folders: upload file-by-file so a partial failure
            # does not roll back the whole tree. Each batch is one
            # commit; the server stores them sequentially.
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
    except OmcError as exc:
        _errprint(str(exc))
        raise typer.Exit(code=1) from exc


__all__ = ["upload_command"]
