"""Server-side file commit into a bare repository.

The `commit_files` helper is the single seam the upload router uses to
land one or more files inside the repo's default branch without going
through the git smart-HTTP push path. The shape matches what the HF
`/api/{owner}/{name}/upload` style endpoints expose (raw multipart files
landed as a single commit on the default branch, with the actor
recorded as the author / committer).

Implementation outline:

    1. Acquire `REPO_LOCKS.acquire(owner, name)` so a concurrent push
       cannot interleave with the worktree we are about to build.
    2. Run the sync helper in a worker thread so the event loop stays
       responsive even for a multi-megabyte commit.
    3. Create a temporary non-bare worktree (sibling of the bare repo).
    4. If the bare repo already has the default branch, `fetch` it into
       the worktree so the new commit has a parent; an empty bare repo
       (the first-ever commit) just stays empty and we initialise the
       branch on commit.
    5. Materialise every uploaded file under `<worktree>/<prefix>/<name>`
       (path traversal rejected before this point).
    6. `porcelain.add` + `porcelain.commit` with `author = committer =
       "<User.username> <email>"` so the commit can be replayed by `git
       log`.
    7. `porcelain.push` the new branch back to the bare repo using a
       local file path - dulwich treats this the same as a network push
       but skips the smart-HTTP negotiation.
    8. Tear down the worktree; the on-disk state mirrors the bare repo
       after `push` so the transient directory is the only thing to
       discard.

dulwich is sync, so the worker thread wrapper is mandatory; the lock is
held across the `to_thread` await so a concurrent push cannot interleave.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from dulwich import porcelain
from dulwich.errors import NotGitRepository
from dulwich.objects import ObjectID
from dulwich.refs import Ref
from dulwich.repo import Repo as _DulwichRepo

from outo_models.repos.storage import REPO_LOCKS, repo_fs_path


@dataclass(frozen=True, slots=True)
class CommitFilesResult:
    """The result of `commit_files`.

    `commit_sha` is the full 40-character hex SHA-1 of the new commit.
    `paths` is the *normalized* on-repo paths that were written, in the
    same order the caller supplied them. `bytes_written` is the sum of
    every file's size BEFORE write - callers add it to user storage
    usage; the post-commit on-disk delta may differ because git packs
    and compresses.
    """

    commit_sha: str
    paths: list[str]
    bytes_written: int


def _default_branch_for(repo: _DulwichRepo, fallback: str) -> str:
    """Return `repo`'s current `HEAD` branch name, or `fallback`.

    A freshly-init'd bare repo has no `HEAD` ref at all (just a
    symbolic-ref stub); in that case we use the caller-provided default
    branch so the first commit creates it.
    """
    try:
        raw = repo.refs.read_ref(Ref(b"HEAD"))
    except (KeyError, ValueError):
        return fallback
    if not raw:
        return fallback
    # HEAD on a populated bare repo is a symbolic ref like
    # `ref: refs/heads/main\n`.
    if raw.startswith(b"ref: "):
        target = raw[len(b"ref: ") :].strip()
        if target.startswith(b"refs/heads/"):
            return target[len(b"refs/heads/") :].decode("ascii", errors="replace")
    return fallback


def _normalize_user_identity(username: str, email: str) -> bytes:
    """Format the author / committer identity dulwich expects.

    Format: `Name <email>`. Username is used as the display name when
    no real name is available, mirroring the convention in the existing
    test seeder helpers (`alice <a@example.com>`).
    """
    return f"{username} <{email}>".encode()


def _join_repo_path(prefix: str, name: str) -> Path:
    """Join an upload prefix + filename into a clean relative Path.

    Both inputs are pre-validated: prefix is a normalized segment list
    (no `..`, no leading `/`), and `name` may contain `/`-separated
    sub-paths (the multipart filename field allows them). Empty
    `name` would have been rejected by the router.
    """
    if not prefix:
        return Path(name)
    return Path(prefix) / name


def _commit_files_sync_inner(
    *,
    fs_path: Path,
    default_branch: str,
    actor_username: str,
    actor_email: str,
    files: Mapping[str, bytes],
    prefix: str,
    message: str,
) -> CommitFilesResult:
    """Sync helper; runs inside `asyncio.to_thread` under the repo lock.

    Raises:
        OSError: on filesystem failure inside the temp worktree.
        NotGitRepository: when the bare repo at `fs_path` does not exist.
    """
    bare = _DulwichRepo(str(fs_path))
    try:
        branch = _default_branch_for(bare, default_branch)
        bare.close()

        # Temp worktree parent lives next to the bare repo so the
        # `shutil.rmtree` lifecycle stays on the same filesystem. We use
        # a deterministic sibling name with `mkdtemp` for uniqueness
        # under concurrent uploads of different repos.
        worktree_parent = fs_path.parent / ".upload-worktree"
        worktree_parent.mkdir(parents=True, exist_ok=True)
        worktree = Path(tempfile.mkdtemp(prefix="wt-", dir=str(worktree_parent)))

        try:
            porcelain.init(str(worktree), bare=False)

            # If the bare repo already has the branch, fetch it so the
            # new commit has a parent (otherwise `push` will create the
            # ref from scratch).
            with contextlib.suppress(Exception):
                # Empty bare repo: fetch has nothing to pull; the first
                # commit creates the branch on push.
                porcelain.fetch(str(worktree), str(fs_path))

            # Second-and-later commits: fetch does NOT create a local branch
            # in the worktree, so point it at the bare tip ourselves — the
            # new commit then has the previous one as its PARENT and the
            # push is a fast-forward instead of a DivergedBranches 500
            # (field failure: every upload after the first one failed).
            # On an empty repo the tip is None and the commit starts the
            # branch from scratch.
            bare_tip = bare.refs.read_ref(Ref(f"refs/heads/{branch}".encode()))
            wt = _DulwichRepo(str(worktree))
            try:
                if bare_tip is not None:
                    wt.refs[Ref(f"refs/heads/{branch}".encode())] = ObjectID(bare_tip)
                    porcelain.checkout(str(worktree), target=branch, force=True)
            finally:
                wt.close()

            bytes_written = 0
            written_paths: list[str] = []
            for rel_name, content in files.items():
                target = worktree / _join_repo_path(prefix, rel_name)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                bytes_written += len(content)
                # Store the canonical on-repo path so the router can
                # return it to the client verbatim.
                canonical = _join_repo_path(prefix, rel_name).as_posix()
                written_paths.append(canonical)

            # Stage everything under the worktree. Paths must include the
            # upload prefix — `files` keys are bare names.
            porcelain.add(
                str(worktree),
                paths=[_join_repo_path(prefix, rel).as_posix() for rel in files],
            )

            identity = _normalize_user_identity(actor_username, actor_email)
            resolved_message = message if message else f"upload: {len(files)} file(s)"

            commit_sha = porcelain.commit(
                str(worktree),
                message=resolved_message.encode("utf-8"),
                author=identity,
                committer=identity,
                author_timestamp=None,  # porcelain fills `now`
                commit_timestamp=None,
                no_verify=True,
                sign=False,
                env={
                    "GIT_AUTHOR_NAME": actor_username,
                    "GIT_AUTHOR_EMAIL": actor_email,
                    "GIT_COMMITTER_NAME": actor_username,
                    "GIT_COMMITTER_EMAIL": actor_email,
                },
            )

            # Push the active branch back to the bare repo. Using a
            # local path sidesteps the smart-HTTP protocol; dulwich
            # treats it the same way.
            active = porcelain.active_branch(str(worktree))
            porcelain.push(
                str(worktree),
                str(fs_path),
                b"refs/heads/" + active + b":refs/heads/" + branch.encode("ascii"),
                force=False,
            )

            return CommitFilesResult(
                commit_sha=commit_sha.decode("ascii"),
                paths=written_paths,
                bytes_written=bytes_written,
            )
        finally:
            # Always tear down the temp worktree; nothing inside it
            # outlives this call because every file we wrote has been
            # pushed into the bare repo above.
            shutil.rmtree(worktree, ignore_errors=True)
    finally:
        with contextlib.suppress(Exception):
            bare.close()


async def commit_files(
    *,
    owner: str,
    name: str,
    default_branch: str,
    actor_username: str,
    actor_email: str,
    files: Mapping[str, bytes],
    prefix: str,
    message: str,
) -> CommitFilesResult:
    """Land `files` in `<owner>/<name>` on `default_branch` as one commit.

    `files` is `{repo_relative_path: content}`. `prefix` is an optional
    directory the files are written under; the final on-repo path is
    `<prefix>/<key>` when prefix is non-empty. `actor_username` /
    `actor_email` populate the commit author + committer so the new
    commit shows up correctly in `git log`. `message` is the commit
    message; empty values fall back to a deterministic placeholder.

    Holds `REPO_LOCKS.acquire(owner, name)` for the duration of the
    work so a concurrent push cannot interleave.

    Raises:
        NotGitRepository: when the bare repo at `repo_fs_path(owner, name)`
            does not exist.
    """
    fs_path = repo_fs_path(owner, name)
    if not fs_path.exists():
        raise NotGitRepository(f"bare repo missing: {owner}/{name}")

    async with REPO_LOCKS.acquire(owner, name):
        return await asyncio.to_thread(
            _commit_files_sync_inner,
            fs_path=fs_path,
            default_branch=default_branch,
            actor_username=actor_username,
            actor_email=actor_email,
            files=files,
            prefix=prefix,
            message=message,
        )


__all__ = ["CommitFilesResult", "commit_files"]
