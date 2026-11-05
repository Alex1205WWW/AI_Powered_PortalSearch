"""Per-job health for the worker: what failed, what is overdue, what is wedged.

Why this exists. Observability in this project was built per SOURCE, not per job: a crawl-shaped job wraps
its work in a `crawl_runs` row and feeds `sources.status`, which the panel banner renders. Ten of the
seventeen scheduled jobs create no row at all - `backup`, `warm_hashes`, `daily_digest`,
`flush_notifications`, `matched_refresh`, `run_queued` and the three `agency_*` jobs - so when one of those
raises, APScheduler logs a line, schedules the next run, and nothing aggregates or surfaces it. Measured
2026-11-04 against a real scheduler: a job that raises every tick keeps running, the scheduler stays up,
and the only trace is one library log line. "Logged and forgotten" is literal.

Two of those ten are the expensive ones. If `backup` starts failing we find out when a restore is needed,
and `flush_notifications` is the exact path that silently destroyed 96 of her alerts on 2026-11-04 while
reporting itself healthy.

State lives in ONE `app_settings` row (`value` is already JSONB, so no migration) and holds **only the
jobs currently in trouble**. A job that succeeds has its entry removed, so an empty dict means "nothing
wrong", writes are near zero in steady state - `run_queued` alone fires every 30 seconds - and the dict is
bounded by the number of broken jobs rather than by time.

No history is kept on purpose: counting consecutive failures, naming the last error and spotting an overdue
job are all answerable from current state. A history table would add a migration, a retention policy and a
page for no buyer.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CrawlRun, Source
from app.services import appsettings, health

log = logging.getLogger(__name__)

KEY = "job_health"

# One definition of "not a blip", shared with source health.
FAILS_BEFORE_DEGRADED = health.FAILURES_BEFORE_DEGRADED

# A job is overdue only if it is later than this AND later than its own interval. The floor is above the
# 600 s `misfire_grace_time` in the scheduler config, so a legitimately misfired-and-rescheduled job is
# never reported; below that we would flag normal behaviour.
OVERDUE_FLOOR_S = 900

# A `crawl_runs` row still `running` after this long means the process died mid-flight: nothing in this
# project runs that long any more, because the RE/MAX backfill works in 20-minute slices.
STALLED_RUN_S = 6 * 3600

WORKER_KEY = "worker"


def read(session: Session) -> dict:
    """{job_id: {fails, error, last_fail}} for the jobs currently in trouble. Empty means healthy."""
    value = appsettings.get(session, KEY, None)
    return dict(value) if isinstance(value, dict) else {}


def _write(session: Session, state: dict) -> None:
    appsettings.put(session, KEY, state)


def record_failure(session: Session, job_id: str, error: str, *, now: datetime | None = None) -> int:
    """-> the new consecutive-failure count for this job."""
    state = read(session)
    entry = state.get(job_id) or {}
    fails = int(entry.get("fails") or 0) + 1
    state[job_id] = {
        "fails": fails,
        "error": str(error)[:500],
        "last_fail": (now or datetime.now(timezone.utc)).isoformat(),
    }
    _write(session, state)
    log.warning("job failed (%dx): %s", fails, error, extra={"job": job_id})
    return fails


def record_success(session: Session, job_id: str) -> bool:
    """Clear a job's trouble entry. -> True if it was recovering, which is the only case that writes."""
    state = read(session)
    if job_id not in state:
        return False  # the steady-state path: no write at all
    fails = int((state.pop(job_id) or {}).get("fails") or 0)
    _write(session, state)
    log.info("job recovered after %d failure(s)", fails, extra={"job": job_id})
    return True


def failing(state: dict, *, threshold: int = FAILS_BEFORE_DEGRADED) -> list[tuple[str, int, str]]:
    """(job_id, fails, error) for jobs past the threshold, worst first."""
    rows = [(job_id, int(e.get("fails") or 0), str(e.get("error") or ""))
            for job_id, e in state.items() if int(e.get("fails") or 0) >= threshold]
    return sorted(rows, key=lambda r: r[1], reverse=True)


def _interval_seconds(job) -> float | None:
    interval = getattr(getattr(job, "trigger", None), "interval", None)
    return interval.total_seconds() if interval is not None else None


def overdue(jobs, *, now: datetime | None = None) -> list[tuple[str, float]]:
    """(job_id, seconds late) for jobs the scheduler should have run and has not.

    [!] A PAUSED job has `next_run_time = None` (verified 2026-11-04). Treating that as infinitely late
    would report every paused job as broken, so it is skipped. A job's own interval is the tolerance, which
    means an infrequent job is not flagged merely for being far off: a 6-hourly sweep must be 6 h late.
    """
    moment = now or datetime.now(timezone.utc)
    late_jobs = []
    for job in jobs:
        next_run = getattr(job, "next_run_time", None)
        if next_run is None:
            continue
        late = (moment - next_run).total_seconds()
        if late > max(OVERDUE_FLOOR_S, _interval_seconds(job) or 0):
            late_jobs.append((job.id, late))
    return sorted(late_jobs, key=lambda r: r[1], reverse=True)


def stalled_runs(session: Session, *, now: datetime | None = None) -> list[tuple[str, str, float]]:
    """(source key, kind, hours stuck) for crawl runs left open - a job that died mid-flight."""
    moment = now or datetime.now(timezone.utc)
    rows = session.execute(
        select(CrawlRun.id, Source.key, CrawlRun.kind, CrawlRun.started_at)
        .join(Source, Source.id == CrawlRun.source_id)
        .where(CrawlRun.status == "running")
    ).all()
    out = []
    for _run_id, key, kind, started in rows:
        if started is None:
            continue
        stuck = (moment - started).total_seconds()
        if stuck > STALLED_RUN_S:
            out.append((key, kind, stuck / 3600))
    return sorted(out, key=lambda r: r[2], reverse=True)


def evaluate(session: Session, jobs, *, now: datetime | None = None) -> list[str]:
    """Check the worker over, move the `worker` source row, and return the problems in Portuguese.

    The verdict lands on a `sources` row with `kind='worker'`, so `health.banner` shows it with no new
    table and no new UI concept - the same trick used for the Telegram channel. Deliberately outside
    `health.ANNOUNCED_KINDS`: a worker fault is not something she can act on, and her phone should not
    buzz about it."""
    problems: list[str] = []
    for job_id, fails, error in failing(read(session)):
        problems.append(f"a tarefa '{job_id}' falhou {fails}x seguidas ({error[:120]})")
    for job_id, late in overdue(jobs, now=now):
        problems.append(f"a tarefa '{job_id}' está atrasada {late / 60:.0f} min")
    for key, kind, hours in stalled_runs(session, now=now):
        problems.append(f"a recolha '{kind}' de {key} ficou presa há {hours:.0f} h")

    worker = session.scalar(select(Source).where(Source.key == WORKER_KEY))
    if worker is not None:
        if problems:
            health.degrade(worker, "; ".join(problems)[:2000])
        else:
            health.mark_ok(worker)
    return problems
