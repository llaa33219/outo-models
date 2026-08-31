"""Spaces runtime status — v1 stub.

Spaces v1 ships the metadata layer only; container runtime is a roadmap
(v2) item. Every space — public or private, brand-new or stale —
therefore reports `PREVIEW_UNAVAILABLE` with a Korean notice and a
pointer at `/docs/spaces`. The stub is intentionally not a placeholder
that can be silently re-shaped into a real implementation: the
`RuntimeState` enum has a single member today and the runtime module
imports nothing that would let a `Repo` leak past the boundary.

The next milestone that adds a real runtime will introduce new
`RuntimeState` members and branch on them inside `runtime_status`; the
public shape (`RuntimeStatus(state, message, docs_url)`) is locked so
that routers do not need to change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from outo_models.db import Repo


class RuntimeState(StrEnum):
    """Lifecycle state the router matches against when rendering a Space.

    `StrEnum` so the value serializes directly into the JSON payload the
    public API exposes. Adding a new state is a deliberate, schema-visible
    change: every router branch must opt into handling it.
    """

    PREVIEW_UNAVAILABLE = "preview_unavailable"


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """What the router renders on the Space detail page.

    `state` drives the template branch; `message` is shown to the user
    (Korean today, but the type stays a plain `str` so i18n can layer on
    later); `docs_url` points at the always-relevant `/docs/spaces` page.
    """

    state: RuntimeState
    message: str
    docs_url: str


def runtime_status(space: Repo) -> RuntimeStatus:
    """Return the runtime status for `space`.

    In v1 the result is constant: every space is `PREVIEW_UNAVAILABLE`
    with a Korean roadmap notice. `space` is accepted so the signature
    matches what v2 will need; it is currently unused. Marked explicitly
    so a future reviewer can see the dependency is on purpose, not an
    oversight — the `Repo` import exists to keep the type signature
    honest (a function that pretended to take a `Repo` but accepted
    `object` would be a wire the next contributor would cut).
    """
    del space  # Unused in v1; reserved for v2's runtime dispatch.
    return RuntimeStatus(
        state=RuntimeState.PREVIEW_UNAVAILABLE,
        # Korean: "v1 런타임 미지원 — 컨테이너 실행은 로드맵(v2) 항목입니다."
        message="v1에서는 런타임이 지원되지 않습니다. 컨테이너 실행은 로드맵(v2) 항목입니다.",
        docs_url="/docs/spaces",
    )


__all__ = ["RuntimeState", "RuntimeStatus", "runtime_status"]