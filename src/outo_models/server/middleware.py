"""ASGI middleware for security headers + rate limiting.

`SecurityHeadersMiddleware` is a pure ASGI middleware (no `BaseHTTPMiddleware`)
because `BaseHTTPMiddleware` buffers the response body and breaks the
git smart-HTTP streaming path — every byte the git service emits would
have to sit in memory before the wrapper could add its headers. Pure
ASGI is the correct choice for any server that handles large or
streamed responses.

The CSP in `_build_csp` is conservative and only needs to allow
inline styles because the bundled Jinja templates inline a single
`<style>` block. Tightening to `'unsafe-inline'` for styles is the
canonical workaround for the templated style block; script-src stays
`'self'` (no inline scripts are ever shipped).

HSTS is suppressed in internal / IP mode (any IP literal or empty
domain). The decision lives in `Settings.is_internal` so the middleware
stays aligned with the Caddyfile renderer and `base_url` — there is
one definition of "this server speaks HTTPS" and three places that
consult it.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from outo_models.config import Settings

# Headers every response MUST carry. Centralised so a future addition
# (e.g. Permissions-Policy for a new feature) lands in one place.
_CSP_HEADER = (
    "default-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "script-src 'self'; "
    # The Pretendard webfont is served from jsdelivr (cf. 디자인.md §4.1).
    # Internal / LAN installs may have no outbound connectivity at all —
    # the @font-face stack falls back to `system-ui`, but the browser
    # still needs to be allowed to *try* the CDN origin.
    "font-src 'self' https://cdn.jsdelivr.net; "
    "base-uri 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
_PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"

# The Space run proxy serves user-owned app content: either an exported
# static site or a reverse-proxied container. The Space page frames it in
# an iframe, so the run path gets SAMEORIGIN + a framing-only CSP. The
# `script-src` / `style-src` allowlist is deliberately dropped there — the
# framed document is the user's own app whose scripts must execute, and
# running untrusted code is already inherent to the Space feature (its
# container executes with the owner's own credentials, in an isolated
# network namespace). Every other path keeps the strict policy.
_RUN_PATH_PREFIX = "/spaces/"
_RUN_XFO = b"SAMEORIGIN"
_RUN_CSP_HEADER = "frame-ancestors 'self'"
_STRICT_XFO = b"DENY"


def _is_space_run_path(path: str) -> bool:
    """`True` for `/spaces/{owner}/{name}/run/...` requests.

    `run` must be its own path segment, so `/spaces/a/runner/x` (a
    lookalike) stays on the strict policy.
    """
    if not path.startswith(_RUN_PATH_PREFIX):
        return False
    segments = path[len(_RUN_PATH_PREFIX) :].split("/")
    return len(segments) >= 3 and bool(segments[0]) and bool(segments[1]) and segments[2] == "run"


class SecurityHeadersMiddleware:
    """Attach security headers to every outgoing response.

    The middleware operates on the outgoing `http.response.start` message
    and prepends the security header pair to whatever headers the wrapped
    application already produced. HSTS is omitted in internal mode (any IP
    literal or empty domain) — a plain-HTTP server must never advertise
    HSTS, otherwise the browser refuses the plain-HTTP request that the
    internal-mode workflow depends on.
    """

    def __init__(self, app: ASGIApp, *, settings: Settings) -> None:
        self._app = app
        self._settings = settings

    def _should_emit_hsts(self) -> bool:
        """`True` when HSTS makes sense for the configured `domain`.

        Internal mode (IP literal / empty domain) is plain HTTP — HSTS
        would make the same browser refuse the very requests the operator
        needs. Real hostnames (DNS-resolvable names that are not IPs) get
        HSTS so the public-facing flow stays locked to HTTPS.
        """
        return not self._settings.is_internal

    def _build_headers(self, *, framed_by_page: bool) -> list[tuple[bytes, bytes]]:
        """Return the security-header list to prepend to every response."""
        xfo = _RUN_XFO if framed_by_page else _STRICT_XFO
        csp = _RUN_CSP_HEADER if framed_by_page else _CSP_HEADER
        headers: list[tuple[bytes, bytes]] = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", xfo),
            (b"referrer-policy", b"strict-origin-when-cross-origin"),
            (b"permissions-policy", _PERMISSIONS_POLICY.encode("ascii")),
            (b"content-security-policy", csp.encode("utf-8")),
        ]
        if self._should_emit_hsts():
            headers.append(
                (
                    b"strict-transport-security",
                    b"max-age=31536000; includeSubDomains",
                )
            )
        return headers

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """ASGI entry point: forward, then mutate the outgoing response."""
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        security_headers = self._build_headers(
            framed_by_page=_is_space_run_path(scope.get("path", ""))
        )
        response_started = False

        async def wrapped_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start" and not response_started:
                raw_headers: list[tuple[bytes, bytes]] = list(message.get("headers", []))
                # Existing headers can override only with the right intent;
                # we deliberately add ours FIRST so app-provided values win.
                message["headers"] = [*security_headers, *raw_headers]
                response_started = True
            await send(message)

        await self._app(scope, receive, wrapped_send)


__all__ = ["SecurityHeadersMiddleware"]
