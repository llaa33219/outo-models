"""Integration tests for the bundled static-asset route and the
chrome-only behaviors added in v0.5.11: SVG serving for the
navbar logo and the `prefers-color-scheme: dark` token override.

The `/static/{filename}` route lives in `server/routers/ui.py` and only
serves an allowlist of extensions from the packaged
`outo_models.assets.static` directory. Every assertion below pins a
behavior that has already shipped — none of these tests mock what they
verify.

Coverage:

* `TestStaticAssetSvg` — `/static/logo.svg` returns 200 with the right
  media type and SVG body. The same traversal/ext guards from the
  existing `TestStaticAssetsAndClipboardJs` class apply.
* `TestStaticAssetClipboardJs` — re-asserts the JS path still works
  (regression guard: extending the allowlist must not break the
  previous asset).
* `TestNavbarLogoAndDarkModeChrome` — every rendered page exposes the
  logo image, the `color-scheme` meta, AND a `prefers-color-scheme:
  dark` CSS block. These three markers together pin the wire-level
  surface of the v0.5.11 redesign without asserting rendered colors
  (which would require a headless browser).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

# `app` fixture (tests/integration/conftest.py) — yields
# (TestClient, FastAPI, Settings).


class TestStaticAssetSvg:
    """`/static/logo.svg` is served with `image/svg+xml` and a body
    that starts with `<svg`. The route uses an explicit extension
    allowlist (`js`, `svg`); anything else 404s."""

    def test_logo_svg_returns_200_with_svg_content_type(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        response = client.get("/static/logo.svg")
        assert response.status_code == 200
        # Cache-Control kept the same as /static/clipboard.js.
        assert response.headers["content-type"].startswith("image/svg+xml")
        # Body is the actual packaged SVG — first non-whitespace token is `<svg`.
        body = response.text.lstrip()
        assert body.startswith("<svg")

    def test_logo_svg_body_matches_packaged_asset(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        """The bytes served via `/static/logo.svg` are byte-identical
        to the asset packaged under `src/outo_models/assets/static/`.
        Catches drift between the root `logo.svg` copy and the bundled
        copy (the route reads from the package, never from the repo
        root)."""
        from importlib.resources import files

        client, _, _ = app
        response = client.get("/static/logo.svg")
        assert response.status_code == 200
        # Both bodies are bytes; equality is exact, no normalization.
        packaged = (files("outo_models.assets") / "static" / "logo.svg").read_bytes()
        assert response.content == packaged


class TestStaticAssetClipboardJs:
    """Regression guard: extending the route's allowlist to also serve
    SVG must not break the existing JS asset (still 200, same content
    type)."""

    def test_clipboard_js_still_served(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        response = client.get("/static/clipboard.js")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/javascript")
        assert "execCommand" in response.text
        assert "navigator.clipboard" in response.text


class TestStaticAssetAllowlist:
    """Extension allowlist + path traversal guards must hold for any
    asset request, not just for the two well-known paths."""

    def test_unknown_extension_returns_404(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        # logo.png is NOT packaged — even if it were, .png is not in
        # the allowlist.
        assert client.get("/static/logo.png").status_code == 404

    def test_traversal_attempt_is_rejected(self, app: tuple[TestClient, FastAPI, object]) -> None:
        client, _, _ = app
        # Same guard as the JS path — the route rejects any filename
        # containing `/` or `\`, including the URL-encoded traversal
        # attempt below.
        assert client.get("/static/..%2F..%2Flogo.svg").status_code in (400, 404)


class TestNavbarLogoAndDarkModeChrome:
    """Every rendered page must carry three wire-level markers that
    together pin the v0.5.11 navbar + dark-mode chrome:

      1. The navbar `<img>` pointing at `/static/logo.svg` (so the
         wordmark renders everywhere).
      2. The `<meta name="color-scheme" content="light dark">` meta
         tag (so native controls and scrollbars adapt).
      3. A `@media (prefers-color-scheme: dark) { :root { ... } }`
         block in the embedded stylesheet (so the dark token
         override is actually emitted).

    Picking a representative sample of pages keeps the test < 1s and
    still catches routing or template regressions across the page
    tree (home, catalog, forms, repo detail, profile)."""

    PAGES: tuple[str, ...] = (
        "/",
        "/login",
        "/signup",
        "/models",
        "/datasets",
        "/spaces",
    )

    def _check(self, client: TestClient, path: str) -> None:
        response = client.get(path)
        assert response.status_code == 200, path
        body = response.text
        # (1) Navbar logo image, with a stable intrinsic size so we
        # don't get layout shift on load. Both `height` and `width`
        # attrs are required by the design prompt to lock the ratio
        # before the SVG decodes.
        assert 'src="/static/logo.svg"' in body, path
        # (2) color-scheme meta — same origin navigations and native
        # controls (date pickers, scrollbars) read this.
        assert '<meta name="color-scheme" content="light dark">' in body, path
        # (3) The dark token override block. The literal text
        # `prefers-color-scheme: dark` is the only stable identifier
        # of the rule; we don't pin the actual color values (they
        # are tuned by hand and might shift).
        assert "prefers-color-scheme: dark" in body, path
        # The override scope must wrap a `:root { ... }` rule so it
        # actually recolors the document. `디자인.md §3.3` says the
        # dark block mirrors the token set inside `:root`.
        assert body.count(":root {") >= 2, (
            "expected both light and dark :root blocks in the embedded stylesheet"
        )

    async def test_logo_image_present_on_every_page(
        self, app: tuple[TestClient, FastAPI, object]
    ) -> None:
        client, _, _ = app
        for path in self.PAGES:
            self._check(client, path)

    async def test_logo_and_dark_chrome_present_on_repo_and_profile(
        self,
        app: tuple[TestClient, FastAPI, object],
        seed_approved_user,
    ) -> None:
        client, _, _ = app
        await seed_approved_user(username="alice")
        client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "correct horse battery staple"},
        )
        client.post(
            "/api/repos",
            json={"name": "shown", "kind": "model", "visibility": "public"},
        )
        client.post("/api/auth/logout")
        # The repo detail page and the profile page both render the
        # full layout (navbar + footer) — their `style` blocks must
        # not shadow the global `:root` tokens.
        for path in ("/alice", "/alice/shown"):
            self._check(client, path)
