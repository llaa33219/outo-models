"""Repo card rendering: README fetch + front-matter parse + safe markdown.

A "card" is the rendered model / dataset / space landing page: the
README text + the structured YAML front-matter HF-style metadata pages
expose (task, datasets, license, tags, ...). The functions here are
read-only over the on-disk bare repo (no worktree checkout) and the
output is HTML with raw HTML characters escaped by mistune so a
malicious README cannot inject script tags into the UI.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

import mistune
import yaml
from dulwich.errors import NotGitRepository
from dulwich.objects import Blob, ObjectID, ShaFile, Tree
from dulwich.refs import Ref
from dulwich.repo import Repo as _DulwichRepo

from outo_models.repos.storage import repo_fs_path

_README_CANDIDATES: tuple[str, ...] = ("README.md", "README.MD", "readme.md")

_MD_RENDERER: mistune.Markdown = mistune.create_markdown(
    escape=True,
    plugins=["strikethrough", "footnotes"],
)


def _decode_blob(blob: Blob) -> str:
    """Return the blob's bytes decoded as utf-8, replacing invalid bytes.

    The README is plain text so `errors="replace"` is the right policy:
    a stray UTF-8 error mid-file would otherwise crash the page render.
    """
    return blob.data.decode("utf-8", errors="replace")


def resolve_tip_sha(repo: _DulwichRepo, branch: str) -> bytes | None:
    """Best-effort tip resolution: default branch, then HEAD, then the
    single branch if the repo has exactly one.

    Users can legitimately push only `master` (or any other name) into a
    repo whose recorded default branch is `main` — and test seeders hit
    exactly this when the platform git config changes the init default
    branch (field failure in CI). Content should still render instead of
    the page collapsing to an empty state.
    """
    try:
        head_sha = repo.refs.read_ref(Ref(f"refs/heads/{branch}".encode()))
    except (KeyError, ValueError):
        head_sha = None
    if head_sha:
        return head_sha
    try:
        _chain, head_sha = repo.refs.follow(Ref(b"HEAD"))
        if head_sha:
            return head_sha
    except (KeyError, ValueError, NotGitRepository):
        pass
    branches = [k for k in repo.refs if k.startswith(b"refs/heads/")]
    if len(branches) == 1:
        try:
            return repo.refs.read_ref(branches[0])
        except (KeyError, ValueError):
            return None
    return None


def _default_branch_tree(repo: _DulwichRepo, branch: str) -> Tree | None:
    """Resolve `branch`'s tip commit and return its root tree, or `None`.

    Returns `None` for an empty repo, a missing branch, or a broken ref
    — every "no card" condition collapses to `None` so callers can render
    the empty card UI without a try/except.
    """
    head_sha = resolve_tip_sha(repo, branch)
    if head_sha is None:
        return None
    try:
        commit = repo[head_sha]
    except (KeyError, NotGitRepository):
        return None
    tree_sha = getattr(commit, "tree", None)
    if not isinstance(tree_sha, bytes) or not tree_sha:
        return None
    try:
        root: ShaFile = repo[tree_sha]
    except (KeyError, NotGitRepository):
        return None
    return root if isinstance(root, Tree) else None


def _find_readme_blob(tree: Tree, lookup: Callable[[ObjectID], ShaFile]) -> tuple[str, str] | None:
    """Return `(name, text)` for the first README candidate present.

    The lookup is case-insensitive over a fixed three-name list so
    `README.md`, `README.MD`, and `readme.md` all resolve. Other
    casings (`Readme.md`, etc.) are intentionally not supported — the
    spec promises exactly those three names.
    """
    for name in _README_CANDIDATES:
        try:
            mode, sha = tree.lookup_path(lookup, name.encode())
        except (KeyError, NotGitRepository):
            continue
        if mode == 0o160000 or mode == 0o040000 | 0o200000:
            continue
        try:
            obj = lookup(sha)
        except (KeyError, NotGitRepository):
            continue
        if not isinstance(obj, Blob):
            continue
        return name, _decode_blob(obj)
    return None


def _read_readme_sync(owner: str, name: str, *, default_branch: str) -> str | None:
    """Sync implementation of `read_readme`; runs in a worker thread."""
    fs_path = repo_fs_path(owner, name)
    if not fs_path.exists():
        return None
    repo = _DulwichRepo(str(fs_path))
    try:
        tree = _default_branch_tree(repo, default_branch)
        if tree is None:
            return None

        def _lookup(sha: ObjectID) -> ShaFile:
            return repo.object_store[sha]

        result = _find_readme_blob(tree, _lookup)
        return None if result is None else result[1]
    finally:
        repo.close()


async def read_readme(owner: str, name: str, *, default_branch: str = "main") -> str | None:
    """Return the README text for `<owner>/<name>` or `None` when missing.

    Reads the default-branch tip's tree directly with dulwich — no
    worktree checkout, no shell-out to the `git` binary. Returns `None`
    when the repo is empty, the branch is missing, or no README file
    exists at the root.
    """
    return await asyncio.to_thread(_read_readme_sync, owner, name, default_branch=default_branch)


@dataclass(frozen=True, slots=True)
class CardMetadata:
    """Structured fields parsed from a README's YAML front-matter.

    `front_matter` carries the raw parsed YAML so future fields are
    reachable without expanding this dataclass; the convenience
    properties (`task`, `datasets`, ...) read the HF-style keys
    expected by the model / dataset / space detail pages.
    """

    front_matter: dict[str, object] = field(default_factory=dict)
    body_html: str = ""

    def _scalar_str(self, key: str) -> str | None:
        value = self.front_matter.get(key)
        return value if isinstance(value, str) else None

    def _list_of_str(self, key: str) -> list[str]:
        value = self.front_matter.get(key)
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [item for item in value if isinstance(item, str)]
        return []

    @property
    def task(self) -> str | None:
        """Free-form task label; mirrors the HF `task` front-matter field."""
        return self._scalar_str("task")

    @property
    def datasets(self) -> list[str]:
        """Dataset identifiers; list or single-string, normalised to `list[str]`."""
        return self._list_of_str("datasets")

    @property
    def base_model(self) -> str | None:
        """Base-model identifier when the card describes a fine-tune / adapter."""
        return self._scalar_str("base_model")

    @property
    def license(self) -> str | None:
        """License label (SPDX id or free-form)."""
        return self._scalar_str("license")

    @property
    def tags(self) -> list[str]:
        """Free-form tags; HF-style list of strings."""
        return self._list_of_str("tags")

    @property
    def language(self) -> list[str]:
        """Languages the card declares (ISO codes or names)."""
        return self._list_of_str("language")


def _extract_front_matter(text: str) -> tuple[dict[str, object], str]:
    """Split a `---`-fenced YAML block from the body; tolerate malformed input.

    Returns `(front_matter, body)`. A missing or broken front matter
    yields an empty dict and the original text as the body — the page
    still renders; the failure is silent because user-supplied READMEs
    are exactly the place where strict parsing breaks the most pages.
    """
    if not text.startswith("---"):
        return {}, text
    # Search for the closing fence on its own line.
    body_start = text.find("\n---", 3)
    if body_start == -1:
        return {}, text
    yaml_block = text[3:body_start].lstrip("\n")
    after_fence = body_start + 4  # skip "\n---"
    remainder = text[after_fence:].lstrip("\n")
    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError:
        return {}, text
    if not isinstance(parsed, dict):
        return {}, text
    return {str(k): v for k, v in parsed.items()}, remainder


def _render_markdown(body: str) -> str:
    """Render `body` as HTML with raw HTML characters escaped.

    `mistune.create_markdown(escape=True, ...)` neutralises `<script>` and
    similar payloads so a malicious README cannot inject JS into the
    UI. The renderer is module-level so we only pay the cost once.
    """
    return str(_MD_RENDERER(body))


def parse_card_metadata(readme_text: str) -> CardMetadata:
    """Parse `readme_text` into structured front-matter + rendered HTML body.

    Empty input yields an empty `CardMetadata` (no frontmatter, no body)
    so callers can render the empty-card UI without special-casing.
    """
    if not readme_text:
        return CardMetadata()
    front_matter, body = _extract_front_matter(readme_text)
    body_html = _render_markdown(body)
    return CardMetadata(front_matter=front_matter, body_html=body_html)


async def read_card(owner: str, name: str, *, default_branch: str = "main") -> CardMetadata | None:
    """Convenience: read the README and parse it in one call.

    Returns `None` when no README exists so callers can distinguish
    "no card" from "card with empty body".
    """
    text = await read_readme(owner, name, default_branch=default_branch)
    if text is None:
        return None
    return parse_card_metadata(text)


__all__ = [
    "CardMetadata",
    "parse_card_metadata",
    "read_card",
    "read_readme",
]
