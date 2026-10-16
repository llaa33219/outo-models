# Administrator guide

This page covers the everyday operational scenarios. For the full flag
reference of every command, see [cli.md](cli.md).

Most of these scenarios are also served by the **web dashboard** at
`/admin` — sign in with an admin account and an "Admin" entry appears in
the profile dropdown. See [§0. Web dashboard](#0-web-dashboard) for the
full story; the CLI (`outo-models admin …`) and the REST API
(`/api/admin/*`) remain the transport for scripts and remote mode.

## 0. Web dashboard

`GET /admin` renders the admin dashboard. It is role-gated: a
non-admin session gets a `403` HTML page. Three tabs, one URL each:

| Tab | URL | Query filters |
| --- | --- | --- |
| Signups (default) | `/admin` or `/admin/signups` | — |
| Users | `/admin/users` | `?status=approved\|pending\|denied\|banned`, `?page=<n>` (50 rows per page) |
| Audit | `/admin/audit` | `?action=<audit action>`, `?limit=<1..500>` (default 100) |

An unknown tab (for example `/admin/nope`) returns
`404 {"error":"not_found"}` — the same JSON shape every unknown repo
page returns. Route registration matters here: `/admin/{tab}` is a
two-segment path and would otherwise be swallowed by the
`/{owner}/{name}` repo catch-all, so the admin routes are registered
before it.

**Mutations** go through `POST /admin/users/{username}/{action}` with
`action` ∈ `approve`, `deny`, `ban`, `unban`, `quota`, `gpu`. Each form
carries the `_csrf` hidden field (double-submit cookie), and every
mutation runs through the same domain services the REST API uses
(`outo_models.auth.approval` for approve / deny / ban / unban,
`_admin_helpers.set_user_quota`, `set_user_gpu_ids`, `clear_user_gpu_ids`
for quota / GPU). An unknown action or user returns `404`.

Every mutation answers with a `303` redirect back to its tab carrying a
flash in the query string:

- success: `?<noun>_saved=1` (`noun` is `signup`, `user`, `quota`, or
  `gpu`, chosen per action)
- failure: `?<noun>_error=<code>&message=<url-quoted text>`, where
  `<code>` is the `OutoError` `.code` (`validation_failed`, `conflict`,
  `forbidden`, `not_found`, …). An unexpected exception is logged and
  flashed as `internal_error`. Failed actions roll the session back, so a
  rejected form changes nothing.

What each tab does:

- **Signups** — the pending queue with an inline Approve button and a
  deny form that takes a reason (max 500 characters; a longer one is a
  `validation_failed` flash, not a half-applied write).
- **Users** — every user with role, status chip, quota (max / used, in
  human units), and GPU assignment. Filter by status, page through with
  `?page=`. Each row has a `<details>` panel holding three editors:
  quota (a human size such as `500GiB`, parsed by the same
  `parse_human_bytes` the CLI uses), GPU ids (comma- or
  whitespace-separated; an empty field clears every assignment), and
  ban / unban with an optional reason.
- **Audit** — newest-first rows with timestamp, action, actor, target,
  detail, and source IP, filtered by `?action=` over the distinct
  actions present in the log. See [§7](#7-inspecting-audit-logs).

Ban rails: an admin cannot ban their own account and cannot ban another
admin. Those rows render the Ban form disabled, and a hand-posted
request is refused with a `forbidden` flash. The status chip vocabulary
is `approved`, `pending`, `denied`, `banned` — the same four values the
REST API and CLI use.

The `/api/admin/*` REST API is unchanged and is still what the
`outo-models admin …` CLI talks to, in both local and remote
(`--api-url`) mode.

## 1. Approving and rejecting signups

After `outo-models setup`, new signups enter as `pending` because
`OUTO_REQUIRE_APPROVAL` (default `true`) is on. Process them like so:

```bash
# 1) Check the queue
sudo outo-models admin pending

# 2) Approve or reject
sudo outo-models admin approve alice
sudo outo-models admin deny bob --reason "email domain mismatch"
```

On approval, `User.status` moves from `pending` to `approved`,
`Approval.decision` is updated, and `AuditLog(action="user.approve")` is
recorded. The rejection reason is stored in `Approval.reason` (max 500
chars).

To turn off the approval requirement (self-service signup), pass
`--no-require-approval` to the setup wizard, or change
`require_approval: false` in `/etc/outo-models/config.yaml` and run
`outo-models restart`. The `signup` API itself stays open, so approval-gated
operations remain the recommended default.

## 2. Banning and unbanning users

```bash
sudo outo-models admin ban carol --reason "spam uploads"
sudo outo-models admin unban carol
```

- `ban` works from `pending` / `approved` / `denied`
- Attempting to ban yourself raises `ForbiddenError`
- Attempting to ban another admin also raises `ForbiddenError`
- Banning an already-banned user raises `ConflictError`

Banned users are rejected on every authentication path (session cookie /
PAT). `git_smart.auth.authorize` raises
`ForbiddenError("Account is not active")`, which causes both `git push` and
`git pull` to fail.

## 3. Resetting passwords

When the operator needs to reset a password, the new value is printed to
stdout **exactly once**, so capture it immediately and deliver it through a
secure channel.

```bash
sudo outo-models admin reset-password alice
# [reset] new password for alice (will not be shown again):
#   AbCdEf_GhIjKlMnOpQrS
```

Remote mode is not supported — passwords must not traverse the network. If
the password is lost, SSH into the server and run this command locally.

## 4. Storage quotas

The default quota is `OUTO_DEFAULT_QUOTA_BYTES` (500 GiB by default). It is
auto-assigned to each new user and can be changed at any time.

```bash
# Inspect (KiB/MiB/GiB chosen automatically)
sudo outo-models admin quota show alice
# [quota] alice: max=10.00 GiB used=2.34 GiB

# Change
sudo outo-models admin quota set alice 50GiB
# [quota] alice: max=50.00 GiB
```

The same limits are editable per user from the dashboard's Users tab
(`/admin/users`, the quota editor in each row's `<details>` panel) — see
[§0. Web dashboard](#0-web-dashboard).

Sizes accept human-readable units (`parse_human_bytes`, which lives in
`outo_models.utils.humanbytes` and is re-exported from
`outo_models.cli`):

- Binary units (KiB, MiB, GiB, TiB, PiB) — base 1024
- Decimal units (KB, MB, GB, TB, PB) — base 1000
- Plain integers (interpreted as bytes)

On push, `check_push_allowed` raises `QuotaExceededError` (status 413)
when `used + incoming > max`, so the push itself fails. See
[git-repos.md](git-repos.md#quota-413) and
[architecture.md](architecture.md#quota-model) for details.

## 5. GPU assignment

Each user can be assigned a free-form list of GPU IDs. From v2, the
assigned GPUs are attached to the container when that user launches a
Space (`nvidia.com/gpu=<id>` CDI device).

```bash
sudo outo-models admin gpu show alice
# [gpu] alice: gpu-0, gpu-1

sudo outo-models admin gpu assign alice gpu-0 gpu-2
sudo outo-models admin gpu clear alice
```

Storage is a JSON array under `web_settings(key="gpu:<username>")`. Every
change records `AuditLog(action="admin.gpu")`.

**Accepted input forms.** The dashboard's GPU editor takes a comma- or
whitespace-separated list, and the CLI takes the same values as
positional arguments. Both a bare index and the `gpu-<index>` form are
accepted, so `0`, `gpu-0`, and `gpu-01` all mean the same device.
Assignments are stored **verbatim** (`list[str]`, whatever you typed, e.g.
`gpu-0`); normalization happens only when the Spaces runtime consumes the
value.

**Normalization at consumption.** The runtime maps each stored string to
an NVIDIA CDI device name: a `gpu-` prefix is stripped when the remainder
is an integer, so `gpu-0`, `gpu-01`, and `0` all become the CDI device
`nvidia.com/gpu=0`. Anything else passes through untouched, which covers
the other valid CDI form `GPU-<uuid>`. Because the stored value stays
verbatim, renaming is not needed: an assignment made before the
normalization rule shipped keeps working.

GPU assignment is also editable per user from the dashboard's Users tab
(`/admin/users`, the GPU editor in each row's `<details>` panel; an empty
field clears every assignment) — see
[§0. Web dashboard](#0-web-dashboard).

Attach conditions and troubleshooting: see
[spaces.md §GPU assignment](spaces.md#gpu-assignment) and
[troubleshooting.md §GPU CDI errors](troubleshooting.md#gpu-cdi-errors).

## 6. Remote mode (`--api-url` + `--token`)

Connect to a remote server with a PAT to run the same commands without
SSH.

```bash
# 1) Mint an admin PAT on the server
#    UI: Settings → Tokens, or
#    API: POST /api/auth/tokens {"name":"ops", "scopes":["read","write"]}
TOKEN=outo.paseto.v4.local.xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 2) From another machine on the same LAN
outo-models admin list \
  --api-url https://models.example.com \
  --token "$TOKEN"

outo-models admin approve alice \
  --api-url https://models.example.com \
  --token "$TOKEN"
```

`--api-url` and `--token` must be passed together. Providing only one is
rejected with
`ConfigError("--api-url and --token must be used together")`.

A remote call works as follows:

1. `outo_models.cli_remote.AdminApiClient` issues the HTTPS request with
   `Authorization: Bearer <PAT>`
2. The server verifies against the argon2id fingerprint stored in
   `/api/auth/tokens`
3. The `require_admin` dependency checks `role == "admin"`
4. The `/api/admin/*` handler runs the actual SQL transaction and records
   the `AuditLog`

The only command **unavailable** in remote mode is
`admin reset-password` — plaintext passwords must never cross the network.

## 7. Inspecting audit logs

Three ways to read the audit log:

- Dashboard: `/admin/audit`, filterable with `?action=<action>` and
  `?limit=<1..500>` (see [§0. Web dashboard](#0-web-dashboard))
- API: `GET /api/admin/audit?limit=100` (Bearer PAT, admin only)
- Direct DB query from the server host:
  `sqlite3 /var/lib/outo-models/db.sqlite3 \
  "SELECT created_at, actor_id, action, target_type, target_id FROM audit_logs \
  ORDER BY id DESC LIMIT 20;"`

The `audit_prune` job runs every day at 02:00 UTC and deletes logs older
than 90 days (`tasks/jobs/audit_prune.py`). Adjust the retention by changing
`prune_audit_logs`'s default or calling
`prune_audit_logs(retention_days=N)` directly.

## 8. Changing the signup policy (`require_approval`)

Change the signup policy during operations like so:

```bash
# Modify the env var on the container and restart
sudo podman stop outo-models
sudo podman rm outo-models
sudo outo-models start   # new env var is applied automatically
# or
sudo outo-models update --image outo-models:stable
```

Alternatively, edit `require_approval` in
`/etc/outo-models/config.yaml`; it takes effect on the next container
start.

## Next steps

- [cli.md](cli.md) — exact flags for every command
- [security.md](security.md) — safety policy for admin PATs
- [git-repos.md](git-repos.md) — how quotas / bans affect git operations
