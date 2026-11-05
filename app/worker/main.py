"""Worker entrypoint: APScheduler 3.x in-process, in-memory job store. Jobs are idempotent and derived
from the database, so nothing needs to be persisted. Run: python -m app.worker.main"""
from __future__ import annotations

import logging
import signal
import threading
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED
from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.db.session import session_scope
from app.logging_setup import setup_logging
from app.worker import jobs

log = logging.getLogger(__name__)


# How often the schedule is re-read from the database. She enables and disables sources in the panel, and
# before this existed nothing happened until someone restarted the worker - a control that silently does
# nothing is worse for a non-technical operator than a documented limitation. The panel quotes this number,
# so if it changes, change the wording in templates/sources.html with it.
RELOAD_MINUTES = 5

# Job ids whose existence and interval come from the database rather than from this file.
DYNAMIC_PREFIXES = ("sweep:", "backfill:")
EMAIL_JOB_ID = "poll_email"


def desired_dynamic_jobs() -> dict[str, tuple]:
    """job id -> (function, interval kwargs, args) for every schedule derived from the database.

    The single definition of the dynamic set: `build_scheduler` uses it at boot and `reconcile_schedules`
    uses it on every reload, so the two can never drift into disagreeing about what should be running."""
    wanted: dict[str, tuple] = {}
    for key, minutes in enabled_portal_schedules():
        wanted[f"sweep:{key}"] = (jobs.sweep_source, {"minutes": minutes, "jitter": 120}, [key])
    for key in catalogue_sources():
        # Resumable by construction: the tick is a no-op once the catalogue has been read through once.
        wanted[f"backfill:{key}"] = (jobs.backfill_source, {"minutes": 20, "jitter": 60}, [key])
    email_minutes = email_schedule()
    if email_minutes:
        wanted[EMAIL_JOB_ID] = (jobs.poll_email, {"minutes": email_minutes}, [])
    return wanted


def _interval_minutes(job) -> float | None:
    """The interval a running job is on, in minutes, or None if it is not an interval job."""
    interval = getattr(job.trigger, "interval", None)
    return interval.total_seconds() / 60 if interval is not None else None


def reconcile_schedules(scheduler) -> dict:
    """Make the running scheduler match the database: add, remove and reschedule the dynamic jobs.

    Only ids in `DYNAMIC_PREFIXES` or `EMAIL_JOB_ID` are ever touched, so the fixed jobs in
    `build_scheduler` cannot be removed by a bad database row. Idempotent: a reload that changes nothing
    reports all zeros and is the normal case."""
    wanted = desired_dynamic_jobs()
    current = {j.id: j for j in scheduler.get_jobs()
               if j.id.startswith(DYNAMIC_PREFIXES) or j.id == EMAIL_JOB_ID}
    added = removed = rescheduled = 0
    for job_id, (func, trigger, args) in wanted.items():
        job = current.get(job_id)
        if job is None:
            scheduler.add_job(func, "interval", args=args, id=job_id, **trigger)
            added += 1
            log.info("schedule added", extra={"job": job_id})
        elif _interval_minutes(job) != float(trigger["minutes"]):
            scheduler.reschedule_job(job_id, trigger="interval", **trigger)
            rescheduled += 1
            log.info("schedule changed to %s min", trigger["minutes"], extra={"job": job_id})
    for job_id in current:
        if job_id not in wanted:
            scheduler.remove_job(job_id)
            removed += 1
            log.info("schedule removed", extra={"job": job_id})
    return {"added": added, "removed": removed, "rescheduled": rescheduled, "running": len(wanted)}


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        timezone=ZoneInfo(settings.timezone),
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
    )
    # Heartbeat is also the job-health evaluator, so it needs the scheduler to read `next_run_time`.
    scheduler.add_job(jobs.heartbeat, "interval", minutes=10, id="heartbeat", args=[scheduler])
    scheduler.add_job(jobs.robots_recheck, "cron", day_of_week="sun", hour=4, minute=0, id="robots_recheck")
    scheduler.add_job(jobs.backup, "cron", hour=3, minute=30, id="backup")
    scheduler.add_job(jobs.run_queued, "interval", seconds=30, id="run_queued")
    scheduler.add_job(jobs.flush_notifications, "interval", minutes=2, id="flush_notifications")
    scheduler.add_job(jobs.matched_refresh, "cron", hour=5, minute=0, id="matched_refresh")
    scheduler.add_job(jobs.warm_hashes, "interval", minutes=15, id="warm_hashes", jitter=120)
    # Agency discovery: four paced jobs, never one per agency site.
    scheduler.add_job(jobs.agency_directory, "cron", day_of_week="mon", hour=4, minute=30, id="agency_directory")
    scheduler.add_job(jobs.agency_websites, "interval", minutes=20, id="agency_websites", jitter=120)
    scheduler.add_job(jobs.agency_profile, "interval", minutes=30, id="agency_profile", jitter=120)
    scheduler.add_job(jobs.agency_watch, "interval", hours=6, id="agency_watch", jitter=300)
    # Ticks often, sends once a day: a restart across her digest hour must not skip the day.
    scheduler.add_job(jobs.daily_digest, "interval", minutes=10, id="daily_digest")
    # The database-derived jobs, and the reload that keeps them matching the panel without a restart.
    for job_id, (func, trigger, args) in desired_dynamic_jobs().items():
        scheduler.add_job(func, "interval", args=args, id=job_id, **trigger)
    scheduler.add_job(reconcile_schedules, "interval", minutes=RELOAD_MINUTES, args=[scheduler],
                      id="reload_schedules")
    scheduler.add_listener(_on_job_event, EVENT_JOB_ERROR | EVENT_JOB_EXECUTED)
    return scheduler


def _on_job_event(event) -> None:
    """Record every job outcome, so a failing job cannot be logged and forgotten.

    Measured 2026-11-04: without this, a job that raises is logged once by APScheduler, rescheduled, and
    nothing aggregates or surfaces it - it can fail every tick for weeks unnoticed. Ten of the seventeen
    jobs create no `crawl_runs` row, so this listener is their only record.

    A success for a job that was not already in trouble writes NOTHING (see `jobhealth.record_success`),
    which matters because `run_queued` fires every 30 seconds. Must never raise: an exception in a listener
    would come from the scheduler's own thread and is not something a failing job should be able to trigger.
    """
    from app.services import jobhealth

    try:
        with session_scope() as session:
            if event.exception is not None:
                jobhealth.record_failure(session, event.job_id, repr(event.exception))
            else:
                jobhealth.record_success(session, event.job_id)
    except Exception:  # noqa: BLE001 - recording a problem must never become a second problem
        log.exception("could not record job outcome", extra={"job": getattr(event, "job_id", "?")})


def enabled_portal_schedules() -> list[tuple[str, int]]:
    """(source key, minutes) for every enabled portal that has an adapter.

    Re-read every `RELOAD_MINUTES` by `reconcile_schedules`, so enabling or disabling a source in the panel
    takes effect on its own. A source without an adapter is skipped deliberately: it can be enabled in the
    panel, but there is no code that could read it."""
    from sqlalchemy import select

    from app.db.models import Source
    from app.db.session import session_scope
    from app.sources import load_adapters
    from app.sources.registry import ADAPTERS

    load_adapters()
    with session_scope() as session:
        rows = session.scalars(select(Source).where(Source.enabled.is_(True), Source.kind == "portal"))
        return [(s.key, s.schedule_minutes or 180) for s in rows if s.key in ADAPTERS]


def catalogue_sources() -> list[str]:
    """Enabled sources whose adapter reads a global catalogue, and therefore need a one-off backfill."""
    from sqlalchemy import select

    from app.db.models import Source
    from app.db.session import session_scope
    from app.sources import load_adapters
    from app.sources.base import is_catalogue
    from app.sources.registry import ADAPTERS

    load_adapters()
    with session_scope() as session:
        rows = session.scalars(select(Source).where(Source.enabled.is_(True), Source.kind == "portal"))
        return [s.key for s in rows if s.key in ADAPTERS and is_catalogue(ADAPTERS[s.key])]


def email_schedule() -> int | None:
    """Poll interval for the Idealista alert mailbox, or None when IMAP is not configured or the source is off."""
    from sqlalchemy import select

    from app.db.models import Source
    from app.db.session import session_scope

    if not settings.imap_user or not settings.imap_password:
        return None
    with session_scope() as session:
        source = session.scalar(select(Source).where(Source.key == "idealista_email", Source.enabled.is_(True)))
        return (source.schedule_minutes or 3) if source else None


def main() -> None:
    setup_logging(settings.log_level)
    scheduler = build_scheduler()
    scheduler.start()
    log.info("worker started with jobs: %s", [j.id for j in scheduler.get_jobs()])
    stop = threading.Event()

    def _shutdown(*_):
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    stop.wait()
    scheduler.shutdown(wait=True)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
