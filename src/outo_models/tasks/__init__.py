"""Background-task scheduling for outo-models.

Public surface:

* `TaskScheduler` — the FastAPI lifespan handler instantiates one of these,
  calls `start()` after the DB engine is ready, and `await shutdown()`
  on lifespan exit. WP-13's server startup imports `TaskScheduler` and
  nothing else from this package.

The job bodies live in `outo_models.tasks.jobs` and are documented as
never-raising so the scheduler loop survives transient blips.
"""

from outo_models.tasks.scheduler import TaskScheduler

__all__ = ["TaskScheduler"]
