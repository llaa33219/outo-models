"""Webhook endpoints — v1 stub.

The webhook surface is intentionally minimal in v1; production integrations
land in v2. We keep the route registered so the URL namespace is stable
for callers that hardcode `/api/webhooks/test`.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/test")
async def test_webhook() -> dict[str, object]:
    """Always returns `{ok: true, note: "v2 feature"}` so CI can ping it."""
    return {"ok": True, "note": "webhooks are a v2 feature"}


__all__ = ["router"]
