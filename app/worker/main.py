"""Worker entrypoint: APScheduler 3.x in-process, in-memory job store. Jobs are idempotent and derived
from the database, so nothing needs to be persisted. Run: python -m app.worker.main"""
from __future__ import annotations

import logging
import signal
import threading
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.logging_setup import setup_logging
from app.worker import jobs

log = logging.getLogger(__name__)


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        timezone=ZoneInfo(settings.timezone),
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
    )
    scheduler.add_job(jobs.heartbeat, "interval", minutes=10, id="heartbeat")
    scheduler.add_job(jobs.robots_recheck, "cron", day_of_week="sun", hour=4, minute=0, id="robots_recheck")
    scheduler.add_job(jobs.backup, "cron", hour=3, minute=30, id="backup")
    scheduler.add_job(jobs.run_queued, "interval", seconds=30, id="run_queued")
    scheduler.add_job(jobs.flush_notifications, "interval", minutes=2, id="flush_notifications")
    for key, minutes in enabled_portal_schedules():
        scheduler.add_job(jobs.sweep_source, "interval", minutes=minutes, args=[key], id=f"sweep:{key}", jitter=120)
    email_minutes = email_schedule()
    if email_minutes:
        scheduler.add_job(jobs.poll_email, "interval", minutes=email_minutes, id="poll_email")
    return scheduler


def enabled_portal_schedules() -> list[tuple[str, int]]:
    """(source key, minutes) for every enabled portal that has an adapter. Read once at startup; restart the
    worker after enabling a source in the panel."""
    from sqlalchemy import select

    from app.db.models import Source
    from app.db.session import session_scope
    from app.sources import load_adapters
    from app.sources.registry import ADAPTERS

    load_adapters()
    with session_scope() as session:
        rows = session.scalars(select(Source).where(Source.enabled.is_(True), Source.kind == "portal"))
        return [(s.key, s.schedule_minutes or 180) for s in rows if s.key in ADAPTERS]


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
