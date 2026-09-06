"""Internal helpers for the `upload` endpoint.

The split keeps the public router (`upload.py`) focused on the multipart
form contract while the auth scope check + path validation live here.
Mirrors the existing `_auth_helpers.py` / `_resolve_helpers.py` pattern
in the routers package.

Nothing outside `upload.py` should import this module.
"""

from __future__ import annotations

import json

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from outo_models.auth.permissions import Scope, has_scope
from outo_models.auth.tokens import match_fingerprint
from outo_models.db import PersonalAccessToken, User
from outo_models.server.deps import (
    _resolve_pat_user,
    _resolve_session_user,
    _settings_for_request,
)

# Per-file size cap. 100 MiB matches what the HF upload endpoint accepts
# for an in-tree file; anything bigger should go through git+LFS. The
# error message points the user at the right protocol.
MAX_FILE_BYTES = 100 * 1024 * 1024


def validate_path(path: str) -> list[str]:
    """Return the normalised path segments or raise `ValidationFailedError`."""
    from outo_models.exceptions import ValidationFailedError

    if path.startswith("/"):
        raise ValidationFailedError("path must be a relative directory")
    cleaned = path.replace("\\", "").strip("/")
    if cleaned in ("", "."):
        return []
    parts = cleaned.split("/")
    for part in parts:
        if not part or part in (".", ".."):
            raise ValidationFailedError(f"invalid path segment in {path!r}")
    return parts


def validate_filename(name: str | None) -> str:
    """Validate a file part's filename; return the cleaned relative path."""
    from outo_models.exceptions import ValidationFailedError

    if not name:
        raise ValidationFailedError("file part is missing a filename")
    cleaned = name.replace("\\", "").strip("/")
    if cleaned in ("", "."):
        raise ValidationFailedError(f"invalid filename: {name!r}")
    parts = cleaned.split("/")
    for part in parts:
        if not part or part in (".", ".."):
            raise ValidationFailedError(f"invalid filename: {name!r}")
    return "/".join(parts)


def _scopes_from_pat(pat: PersonalAccessToken) -> set[Scope]:
    """Decode `pat.scopes` (JSON list) into a `Scope` enum set.

    Unknown scope names are silently ignored so a token with a renamed
    scope keeps working until the user re-mints.
    """
    try:
        decoded = json.loads(pat.scopes)
    except (TypeError, ValueError):
        return set()
    out: set[Scope] = set()
    for value in decoded:
        if not isinstance(value, str):
            continue
        try:
            out.add(Scope(value))
        except ValueError:
            continue
    return out


async def resolve_principal(
    *,
    db: AsyncSession,
    request: Request,
) -> User | None:
    """Resolve the request principal and verify the PAT scope.

    Session cookies authenticate without a scope check (the user is
    already logged in via the UI). Bearer PATs must carry the `write`
    scope or any of its resource-prefixed aliases (`repos:write`). The
    function returns `None` when neither resolves — the caller raises
    `UnauthorizedError`.
    """
    settings = _settings_for_request(request)
    authorization = request.headers.get("authorization")

    sess_user_id = _resolve_session_user(request, settings)
    if sess_user_id is not None:
        user = (await db.execute(select(User).where(User.id == sess_user_id))).scalar_one_or_none()
        if user is not None and user.is_active:
            return user

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if token:
            user_id = await _resolve_pat_user(db, bearer=token)
            if user_id is not None:
                pats = (
                    (
                        await db.execute(
                            select(PersonalAccessToken).where(
                                PersonalAccessToken.user_id == user_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                granted_scopes: set[Scope] = set()
                for pat in pats:
                    if pat.is_expired:
                        continue
                    if not match_fingerprint(pat.fingerprint_hash, token):
                        continue
                    granted_scopes.update(_scopes_from_pat(pat))
                    break
                if has_scope(granted_scopes, Scope.WRITE):
                    user = (
                        await db.execute(select(User).where(User.id == user_id))
                    ).scalar_one_or_none()
                    if user is not None and user.is_active:
                        return user

    return None


__all__ = [
    "MAX_FILE_BYTES",
    "resolve_principal",
    "validate_filename",
    "validate_path",
]
