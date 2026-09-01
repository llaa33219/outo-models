"""Walk recent commits in a bare repository.

`recent_revisions` is the read-side counterpart to `create_repo` /
`delete_repo`: it answers "what commits are in this repo?" directly from the
on-disk bare repo, never raising for a missing or empty repo.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from dulwich.objects import ObjectID
from dulwich.refs import Ref
from dulwich.repo import Repo as _DulwichRepo

from outo_models.repos.storage import repo_fs_path

_DEFAULT_BRANCH = "main"
_MAX_LIMIT = 200


@dataclass(frozen=True, slots=True)
class RevisionInfo:
    """One commit read out of a bare repository.

    Mirrors the columns that routers will render in the activity feed /
    repo-detail page. `commit_sha` is the full 40-character hex SHA-1.
    """

    commit_sha: str
    message: str
    author: str
    committed_at: datetime


def _read_revisions(path: Path, limit: int) -> list[RevisionInfo]:
    """Sync walker; returns `[]` for missing / empty / branchless repos.

    Never raises for the contractually defined empty cases so callers do
    not need a try/except to render the empty state. Any other dulwich
    error propagates as-is.
    """
    if not path.exists():
        return []

    repo = _DulwichRepo(str(path))
    ref_name = Ref(f"refs/heads/{_DEFAULT_BRANCH}".encode())
    tip_sha = repo.refs.read_ref(ref_name)
    if tip_sha is None:
        # No commits on the default branch yet (freshly-init'd bare repo,
        # or the operator pushed only to a different branch). Both cases
        # collapse to "no activity".
        return []

    results: list[RevisionInfo] = []
    walker = repo.get_walker(include=[ObjectID(tip_sha)], max_entries=limit)
    for entry in walker:
        commit = entry.commit
        committed_at = datetime.fromtimestamp(commit.commit_time, tz=UTC)
        results.append(
            RevisionInfo(
                commit_sha=commit.id.decode("ascii"),
                message=commit.message.decode("utf-8", errors="replace"),
                author=commit.author.decode("utf-8", errors="replace"),
                committed_at=committed_at,
            )
        )
    return results


async def recent_revisions(owner: str, name: str, limit: int = 20) -> list[RevisionInfo]:
    """Return the most recent commits in `owner/name`, newest first.

    `limit` is clamped to a sane upper bound (200) so a buggy caller cannot
    ask for a multi-megabyte history dump. Never raises for a missing or
    empty repo: returns `[]` instead so routers can render the empty state
    without a try/except.
    """
    safe_limit = max(0, min(limit, _MAX_LIMIT))
    return await asyncio.to_thread(_read_revisions, repo_fs_path(owner, name), safe_limit)


__all__ = ["RevisionInfo", "recent_revisions"]
