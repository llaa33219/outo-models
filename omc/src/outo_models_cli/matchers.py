"""Include / exclude glob filter.

`fnmatch.fnmatch` does the heavy lifting; this module adds the two
behaviours the CLI actually needs:

    * Matching is done against the *path inside the repo* (forward-slash
      separated), not the basename — `*.safetensors` matches
      `weights/model.safetensors` because fnmatch's `*` crosses `/`.
      That's the documented `huggingface_hub` behaviour and matches
      every existing CI script that filters model files.
    * Patterns may include leading `./` or trailing `/` for ergonomic
      reasons; both are stripped before matching so the caller never
      has to think about them.
    * An empty pattern list is a no-op (everything passes include, nothing
      is excluded) — a `--include ""` typo from the shell must not
      silently download zero files.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Final

# Paths in a git tree are forward-slash separated on every platform the
# CLI targets. We normalise backslashes to forward slashes before matching
# so a Windows shell that happened to pass a `path\to\file` glob still
# works against the on-disk forward-slash form.
_PATH_SEP: Final = "/"


def _normalize(pattern: str) -> str:
    """Strip `./` prefixes and trailing slashes; backslashes → forward slashes."""
    cleaned = pattern.strip().replace("\\", _PATH_SEP)
    while cleaned.startswith(f".{_PATH_SEP}"):
        cleaned = cleaned[2:]
    while cleaned.startswith(_PATH_SEP):
        cleaned = cleaned[1:]
    while cleaned.endswith(_PATH_SEP):
        cleaned = cleaned[:-1]
    return cleaned


def matches(path: str, pattern: str) -> bool:
    """Return True iff `path` matches `pattern` (after normalization)."""
    return fnmatch(path, _normalize(pattern))


def passes(
    path: str,
    *,
    include: list[str] | None = None,
    exclude: list[str] | None = None,
) -> bool:
    """Decide whether `path` survives the include / exclude filter.

    Excludes are checked first so a file that is explicitly excluded
    never wins by also matching an include glob. A file passes when:

        * it matches at least one `--include` pattern (if any), AND
        * it matches no `--exclude` pattern.

    With no patterns at all, every path passes — useful for unit tests
    that exercise the streaming logic without caring about globs.
    """
    inc = include or []
    exc = exclude or []
    if inc and not any(matches(path, p) for p in inc):
        return False
    return not any(matches(path, p) for p in exc)


__all__ = ["matches", "passes"]
