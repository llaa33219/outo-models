"""Multipart upload via `POST /api/repos/{owner}/{name}/upload`."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import BinaryIO

import httpx

from outo_models_cli.api import (
    UploadResult,
    display_filename,
    send,
    unwrap,
)
from outo_models_cli.errors import BadResponseError, map_response_error
from outo_models_cli.http import TIMEOUT_COMMIT


def upload(
    client: httpx.Client,
    *,
    owner: str,
    name: str,
    files: Iterable[Path],
    path_in_repo: str = "",
    message: str | None = None,
    file_root: Path | None = None,
    file_handles: Iterable[BinaryIO] | None = None,
) -> UploadResult:
    """Multipart upload of one or many files.

    Args:
        owner / name: Target repo coordinates.
        files: Paths to local files. The filename of each multipart part
            is the *basename* of the path, with the in-repo `path` prefix
            applied by the server using the multipart `path` field.
        path_in_repo: Subdirectory inside the repo where files land
            (server applies it to every filename).
        message: Optional commit message.
        file_root: When set, the in-repo subpath is computed relative to
            this root (used for folder uploads where the user wants
            `dir/file.txt` rather than `file.txt` inside the destination).
        file_handles: Optional iterable of pre-opened binary handles; the
            caller is responsible for closing them. Pass `None` to have
            this function open the paths itself.

    The caller must pre-validate that no single file exceeds
    100 MiB; this function trusts the caller and does not re-check
    (re-checking after opening the file would mean reading the file's
    bytes into Python just to count them).
    """
    opens_handles = file_handles is None
    opened: list[BinaryIO] = []
    try:
        if opens_handles:
            opened = [f.open("rb") for f in files]
            handles = opened
        elif file_handles is not None:
            handles = list(file_handles)
        else:
            handles = []

        multipart_files: list[tuple[str, tuple[str, BinaryIO, str]]] = []
        for idx, handle in enumerate(handles):
            filename = handle.name
            display_name = display_filename(filename, file_root=file_root, idx=idx)
            multipart_files.append(
                ("files", (display_name, handle, "application/octet-stream")),
            )

        # httpx treats `data=[(name, value)]` as a raw (name, value) iterator,
        # NOT as multipart form fields. Build a dict so the keys become
        # `Content-Disposition: form-data; name=...` parts of the envelope.
        fields: dict[str, str] = {}
        if path_in_repo:
            fields["path"] = path_in_repo
        if message:
            fields["message"] = message

        response = send(
            client,
            "POST",
            f"/api/repos/{owner}/{name}/upload",
            files=multipart_files,
            data=fields,
            timeout=TIMEOUT_COMMIT,
        )
        if response.status_code >= 400:
            raise map_response_error(response)
        payload = unwrap(response)
        commit_sha = payload.get("commit_sha")
        files_acked = payload.get("files", [])
        if not isinstance(commit_sha, str) or not commit_sha:
            raise BadResponseError("`/upload` response is missing `commit_sha`.")
        if not isinstance(files_acked, list):
            files_acked = []
        return UploadResult(
            commit_sha=commit_sha,
            files=[str(name) for name in files_acked],
            message=(str(payload["message"]) if isinstance(payload.get("message"), str) else None),
        )
    finally:
        for handle in opened:
            handle.close()


__all__ = ["upload"]
