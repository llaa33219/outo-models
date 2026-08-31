"""Periodic job bodies wired into `outo_models.tasks.scheduler.TaskScheduler`.

Each job body is documented as never-raising so the scheduler loop keeps
ticking across transient blips:

* `cert_renewal_job` — daily TLS cert health check + Caddy reload nudge.
* `quota_reconcile_job` — hourly per-user storage usage recompute.
* `prune_audit_logs` — daily retention prune for `audit_logs`.

The scheduler never calls these directly with positional `Settings` /
factory arguments for the prune job; it uses `functools.partial`-free
positional `add_job(args=...)` instead, so each entry point here exposes
the smallest signature the scheduler needs.
"""

from outo_models.tasks.jobs.audit_prune import prune_audit_logs
from outo_models.tasks.jobs.quota_reconcile import quota_reconcile_job
from outo_models.tasks.jobs.renewal import cert_renewal_job

__all__ = [
    "cert_renewal_job",
    "prune_audit_logs",
    "quota_reconcile_job",
]
