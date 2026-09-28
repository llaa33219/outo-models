# Posts

Posts are a **site-wide** social surface: a Thread / X-style feed where
any approved user can publish short notes or long-form markdown articles,
react with a fixed emoji palette, and comment on other people's posts.
Posts are independent of the per-repo community tab
(`/{owner}/{name}/community`) — the per-repo tab keeps the threaded
reply chain it has always had, while posts are flat (no replies in v0.5).

Posts are *not* a way to share git content (the model card, dataset
README, and Space front page stay in their repos). Posts are a way to
publish short commentary, announce releases, write blog-style articles,
and link readers to a repo for the long-form material.

## Pages

| Path | Purpose | Login |
| --- | --- | --- |
| `GET /posts` | Feed (newest 50, mixed short + long), 900px-wide | public |
| `GET /posts/new` | Long-form markdown editor page | required |
| `GET /posts/preview` | Editor page (empty) | required |
| `POST /posts/preview` | Render the draft through the sanitizer, re-render the editor with the preview + preserved draft | required |
| `POST /posts/new` | Create (CSRF-protected) | required |
| `GET /posts/{id}` | Detail + reactions + comments | public |
| `POST /posts/{id}/react` | Toggle reaction (CSRF-protected) | required |
| `POST /posts/{id}/comments` | Add comment (CSRF-protected) | required |
| `POST /posts/{id}/delete` | Delete post (CSRF-protected) | author or admin |
| `POST /posts/{id}/comments/{cid}/delete` | Delete comment (CSRF-protected) | author or admin |

The feed and the detail page cap their content at 900px. Every popup
on the posts surface — the per-post **Comments** modal, the feed's
**New post** modal (short-form only), and the reaction **picker**
popover — is a native `<details>/<summary>` disclosure styled as a
fixed-position overlay. No JavaScript anywhere (CSP `script-src
'self'`); a second `<summary>` inside each overlay acts as the close
control because clicking any summary of an open `<details>` closes it.

Post tiles are clickable: the title and body preview link to the
post's detail page. Clicking a post title/body navigates; the
reaction chips, picker, and comment controls sit outside the link so
they remain individually clickable.

The long-form editor is a dedicated page: title input, large body
textarea, a server-side **Preview** action (POST `/posts/preview`
renders the draft through the same sanitized-markdown pipeline the
model card uses and returns the editor with the preview plus the
preserved draft), and **Publish** (POST `/posts/new` with
`kind=long`). A validation failure on a short-form submit from the
feed modal re-renders the feed with the modal open (`<details open`)
so the error shows in context.

## Post kinds

| Kind | Title | Body cap | Render |
| --- | --- | --- | --- |
| `short` | none | 2000 chars | escaped plain text + `<br>` for line breaks |
| `long` | required (≤ 200 chars) | 50000 chars | sanitized markdown via `repos.card` |

The DB CHECK on `posts` enforces the kind/title asymmetry
(`(kind = 'long' AND title IS NOT NULL AND length(title) > 0) OR (kind = 'short' AND title IS NULL)`),
so a hand-crafted INSERT cannot produce a long-without-title row.

The long-body markdown sanitizer mirrors the model card pipeline
(`outo_models.repos.card.parse_card_metadata`): raw HTML is allowed,
but `<script>`, `<style>`, `<iframe>`, `<object>`, `<embed>`, `<form>`,
`<link>`, `<meta>`, `on*` event handlers, and `javascript:` /
`vbscript:` / `data:` URL schemes are stripped server-side. The CSP
(`script-src 'self'`) is the second line of defense; the sanitizer is
the first.

## Reactions

A fixed palette of 26 emoji, locked at the model layer
(`REACTION_PALETTE` in `outo_models.db.models.posts`). Adding a glyph
is a deliberate contract change (migration 0006 expanded the palette
from 8 to 26 glyphs in v0.6.1).

| | | | |
| --- | --- | --- | --- |
| 👍 (thumbs up) | 👎 (thumbs down) | ❤️ (red heart) | 🚀 (rocket) |
| 🎉 (party / tada) | 😮 (open mouth) | 😕 (confused) | 👀 (eyes) |
| 🫪 (face with bags under eyes) | 🤓 (nerd face) | 🧐 (face with monocle) | 😎 (sunglasses) |
| ‼️ (double exclamation) | ⁉️ (exclamation question) | ❓ (red question) | ❗ (red exclamation) |
| ✅ (check mark button) | ❌ (cross mark) | 👏 (clapping hands) | 🍄 (mushroom) |
| 🐞 (lady beetle) | ⭐ (star) | 🌟 (glowing star) | 🔥 (fire) |
| 💧 (droplet) | 🌝 (full moon face) | | |

Reactions are per-user toggles: `POST /posts/{id}/react` with an emoji
adds the reaction if missing, removes it if present. The DB enforces
uniqueness via `UNIQUE(post_id, user_id, emoji)` and the value
palette via `CHECK(emoji IN (...))`.

The UI is GitHub/Discord-style. The full palette is NEVER laid out as
a button row: a post shows only its **applied reactions** as chips
(`emoji count`), each chip a toggle form for that emoji, with the
viewer's own reaction highlighted. Adding a new reaction opens the
**picker** popover (a `<details>` disclosure triggered by a `+`
capsule) listing all 26 glyphs as compact toggle buttons. The home
page's compact post tiles show the applied-chips summary only — no
picker, no modals on the home grid.

## Comments

Flat, single-level comments on a post (no replies). 4000-char cap,
author + post relationships, chronological order. On the feed, each
post's **Comments** button opens a modal (`<details>` overlay) that
shows the comments immediately — the feed eager-loads comments for
all listed posts in one `post_id IN (...)` query (latest 50 per post)
— plus the composer inside the modal. Submitting from the modal
returns to the feed with that post's modal re-opened. The detail page
renders comments inline under the `#comments` anchor. Comments can be
deleted by the comment author or by any admin. A deleted post
cascades its reactions and comments via the service layer (the FK
`ON DELETE CASCADE` is declared but SQLite does not enforce it
without `PRAGMA foreign_keys = ON`).

## Home page mix

The home page (`GET /`) is no longer a single "Public repositories"
listing — the kind catalogs at `/models`, `/datasets`, and `/spaces`
serve that role. Instead the home tiles four kinds:

| Tile | Source |
| --- | --- |
| Trending posts | top by reaction count over the last 7 days; falls back to most-recent when no reactions exist in the window |
| Top models | `Repo.downloads_count` desc (an **all-time** counter — the git smart-HTTP upload-pack path increments `downloads_count`; no per-event timestamp is persisted) |
| Top datasets | same `Repo.downloads_count` ordering, scoped to `kind = 'dataset'` |
| Recently updated Spaces | `Repo.updated_at` desc for `kind = 'space'` — the Spaces v2 runtime does **not** persist a `running_since` timestamp; "longest-running" is approximated by most-recently-updated, and the tile carries a footnote making that explicit |

Each tile is a BLP square tile (2px border, no radius, instant border
hover per `디자인.md §1.3 / §5.1`) with a small kind pill ("Post" /
"Model" / "Dataset" / "Space") so the mix reads at a glance.

## Privacy

Posts are always readable (the URL is a numeric post id). The
linked-repo chip is the only thing gated on repo visibility:

* Linked to a **public** repo → chip visible to everyone.
* Linked to a **private** repo → chip visible only to the repo owner
  and admins. The post body stays readable; the chip is simply hidden
  from strangers so a private repo's existence is not leaked through a
  post detail.

Posting about a private repo from the composer requires the same
visibility: a stranger trying to attach `owner/private-repo` gets a
validation error and the post is not created. The owner and admins
can attach their own private repos.

The home tile lists only public repos in the top-models /
top-datasets / running-spaces sections (matching the kind catalogs'
visibility rules).

## Profile page

Every user profile (`/{username}`) lists the user's posts in a "Posts"
section below Models / Datasets / Spaces. Newest first, limit 20, the
same compact BLP tile shape as the home tiles. Private-repo-linked
posts surface the chip only to viewers who can see the repo, exactly
as on the feed.

## DB schema

Three new tables in migration `0005_posts`:

* `posts` — `id`, `author_id`, `kind`, `title`, `body`, `repo_id`,
  `created_at`, `updated_at`; CHECK on `kind`; CHECK on
  title/kind consistency; indexes on `author_id`, `repo_id`,
  `created_at`; FK `repo_id → repos.id ON DELETE SET NULL`.
* `post_reactions` — `id`, `post_id`, `user_id`, `emoji`,
  `created_at`; UNIQUE(post_id, user_id, emoji); CHECK(emoji IN
  palette); FK `post_id → posts.id ON DELETE CASCADE`; indexes on
  `post_id` and `(post_id, emoji)`.
* `post_comments` — `id`, `post_id`, `author_id`, `body`,
  `created_at`; FK `post_id → posts.id ON DELETE CASCADE`; indexes on
  `post_id` and `author_id`.

The migration runs as part of the standard `outo-models update`
upgrade path; no operator intervention is required.

## Limits and validation

| Field | Limit | Where enforced |
| --- | --- | --- |
| Short body | 2000 chars | `POST_BODY_SHORT_MAX` (service layer + 422 on overflow) |
| Long body | 50000 chars | `POST_BODY_LONG_MAX` (service layer + 422 on overflow) |
| Title | 200 chars | `POST_TITLE_MAX` |
| Comment body | 4000 chars | `POST_COMMENT_BODY_MAX` |
| Emoji | one of the palette | DB CHECK + service layer validation |
| Repo link | `owner/name` slug pair | service layer validation + visibility check |
