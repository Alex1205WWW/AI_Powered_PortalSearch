"""The worker must follow the panel without a restart (T1.4).

`enabled_portal_schedules()` and `email_schedule()` used to be read once, inside `build_scheduler()`. So
disabling Imovirtual in the panel changed a row in the database and nothing else: the sweep kept running
until someone restarted the container. For a non-technical operator a control that silently does nothing
is worse than a documented limitation, which is why this is a real defect and not a nicety.

Driven against a stub scheduler so the reconcile rules are pinned with no APScheduler and no database.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.worker import main as worker_main


class _Job:
    def __init__(self, job_id, minutes=None):
        self.id = job_id
        self.trigger = type("T", (), {"interval": timedelta(minutes=minutes)})() if minutes else object()


class _Scheduler:
    """Records what was asked of it, so the assertions are about behaviour, not internals."""

    def __init__(self, jobs=()):
        self._jobs = list(jobs)
        self.added, self.removed, self.rescheduled = [], [], []

    def get_jobs(self):
        return list(self._jobs)

    def add_job(self, func, trigger_type, args=None, id=None, **kwargs):
        self.added.append((id, kwargs.get("minutes"), tuple(args or ())))
        self._jobs.append(_Job(id, kwargs.get("minutes")))

    def remove_job(self, job_id):
        self.removed.append(job_id)
        self._jobs = [j for j in self._jobs if j.id != job_id]

    def reschedule_job(self, job_id, trigger=None, **kwargs):
        self.rescheduled.append((job_id, kwargs.get("minutes")))


@pytest.fixture
def wanted(monkeypatch):
    """Control what the database says, without a database."""
    state = {"portals": [("imovirtual", 180)], "catalogues": ["remax"], "email": 3}
    monkeypatch.setattr(worker_main, "enabled_portal_schedules", lambda: state["portals"])
    monkeypatch.setattr(worker_main, "catalogue_sources", lambda: state["catalogues"])
    monkeypatch.setattr(worker_main, "email_schedule", lambda: state["email"])
    return state


def test_a_newly_enabled_source_starts_without_a_restart(wanted):
    scheduler = _Scheduler([_Job("heartbeat", 10)])
    result = worker_main.reconcile_schedules(scheduler)
    assert result["added"] == 3 and result["removed"] == 0
    assert {job_id for job_id, _m, _a in scheduler.added} == {"sweep:imovirtual", "backfill:remax", "poll_email"}
    # the source key has to reach the job, or the sweep would not know what to crawl
    assert ("sweep:imovirtual", 180, ("imovirtual",)) in scheduler.added


def test_a_disabled_source_stops_without_a_restart(wanted):
    scheduler = _Scheduler([_Job("sweep:imovirtual", 180), _Job("backfill:remax", 20), _Job("poll_email", 3)])
    wanted["portals"] = []
    result = worker_main.reconcile_schedules(scheduler)
    assert scheduler.removed == ["sweep:imovirtual"]
    assert result["removed"] == 1 and result["added"] == 0


def test_a_changed_interval_is_applied_without_a_restart(wanted):
    scheduler = _Scheduler([_Job("sweep:imovirtual", 180), _Job("backfill:remax", 20), _Job("poll_email", 3)])
    wanted["portals"] = [("imovirtual", 240)]
    result = worker_main.reconcile_schedules(scheduler)
    assert scheduler.rescheduled == [("sweep:imovirtual", 240)]
    assert result["rescheduled"] == 1 and scheduler.removed == [] and scheduler.added == []


def test_a_reload_that_changes_nothing_does_nothing(wanted):
    """The normal case. It must be silent, not churn the jobs every five minutes."""
    scheduler = _Scheduler([_Job("sweep:imovirtual", 180), _Job("backfill:remax", 20), _Job("poll_email", 3)])
    result = worker_main.reconcile_schedules(scheduler)
    assert (result["added"], result["removed"], result["rescheduled"]) == (0, 0, 0)
    assert scheduler.added == [] and scheduler.removed == [] and scheduler.rescheduled == []


def test_the_fixed_jobs_are_never_touched(wanted):
    """A bad database row must not be able to remove the backup, the digest or the alert flush."""
    fixed = ["heartbeat", "robots_recheck", "backup", "run_queued", "flush_notifications",
             "matched_refresh", "warm_hashes", "agency_directory", "agency_websites", "agency_profile",
             "agency_watch", "daily_digest", "reload_schedules"]
    scheduler = _Scheduler([_Job(j, 10) for j in fixed])
    wanted.update(portals=[], catalogues=[], email=None)
    worker_main.reconcile_schedules(scheduler)
    assert scheduler.removed == [], "a fixed job was removed by the reload"


def test_imap_not_configured_means_no_email_job(wanted):
    scheduler = _Scheduler([_Job("poll_email", 3)])
    wanted["email"] = None
    worker_main.reconcile_schedules(scheduler)
    assert scheduler.removed == ["poll_email"]


def test_boot_and_reload_cannot_disagree_about_what_should_run():
    """Both read `desired_dynamic_jobs()`. Two separate lists would drift, and the symptom would be a job
    that only exists until the first reload - or only after it."""
    import inspect

    build = inspect.getsource(worker_main.build_scheduler)
    reconcile = inspect.getsource(worker_main.reconcile_schedules)
    assert "desired_dynamic_jobs()" in build and "desired_dynamic_jobs()" in reconcile
    assert "enabled_portal_schedules()" not in build, "boot must not build its own list"


def test_the_panel_promise_matches_the_actual_reload_interval():
    """templates/sources.html tells her the change applies within N minutes; N comes from the code."""
    from pathlib import Path

    template = Path(worker_main.__file__).parents[1] / "web" / "templates" / "sources.html"
    assert "reload_minutes" in template.read_text(encoding="utf-8")
    assert worker_main.RELOAD_MINUTES == 5
