"""Scheduled jobs. Every job: open a session, take the advisory lock, do idempotent work, log one line."""
from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import settings
from app.db.session import session_scope
from app.sources import robots
from app.worker.locks import advisory_lock

log = logging.getLogger(__name__)


def heartbeat() -> None:
    log.info("worker alive", extra={"job": "heartbeat"})


def run_queued() -> None:
    """Execute queued initial runs (created when criteria are added in the panel or by the bot)."""
    from app.services import runs

    with session_scope() as session, advisory_lock("run_queued") as ok:
        if not ok:
            return
        done = runs.run_queued(session)
        if done:
            log.info("queued runs executed: %d", done, extra={"job": "run_queued"})


def sweep_source(source_key: str) -> None:
    """Incremental sweep of one portal, then matching, events and Telegram flush."""
    from app.services import runs

    with session_scope() as session, advisory_lock(f"sweep:{source_key}") as ok:
        if not ok:
            log.info("sweep already running, skipped", extra={"job": "sweep", "source": source_key})
            return
        runs.execute_sweep(session, source_key)


def backfill_source(source_key: str) -> None:
    """The one-off historical pass over a catalogue source (RE/MAX). Runs on a timer so that a crash, a
    deploy or a reboot simply resumes it at the next tick; it returns immediately once the catalogue has
    been read through once."""
    from sqlalchemy import select

    from app.db.models import Source
    from app.services import runs

    with session_scope() as session, advisory_lock(f"backfill:{source_key}") as ok:
        if not ok:
            return
        source = session.scalar(select(Source).where(Source.key == source_key))
        if source is None or not source.enabled:
            return
        if runs.backfill_done(session, source):
            return
        run = runs.execute_backfill(session, source_key)
        log.info("backfill tick: status=%s new=%d updated=%d at %s", run.status, run.listings_new,
                 run.listings_updated, (run.cursor or {}).get("index"), extra={"job": "backfill", "source": source_key})


def warm_hashes() -> None:
    """Keep the dedup photo-hash cache warm in the background, in small bounded bites."""
    from app.services import properties as prop_svc

    with session_scope() as session, advisory_lock("warm_hashes") as ok:
        if not ok:
            return
        stats = prop_svc.warm_hashes(session, limit=400)
        if stats["hashed"] or stats["from_cache"]:
            log.info("photo hashes warmed: %s", stats, extra={"job": "warm_hashes"})


def matched_refresh() -> None:
    """Re-read the detail page of every matched listing: price history, removal, and the extra fields the
    search results never carry (Roadmap_M1.md section 3 item 7)."""
    from app.services import refresh

    with session_scope() as session, advisory_lock("matched_refresh") as ok:
        if not ok:
            return
        stats = refresh.refresh_matched(session)
        log.info("matched refresh: %s", stats, extra={"job": "matched_refresh"})


def poll_email() -> None:
    """Read new Idealista alert emails from the dedicated mailbox and ingest their listings."""
    from app.services import runs

    with session_scope() as session, advisory_lock("poll_email") as ok:
        if not ok:
            return
        runs.execute_email_poll(session)


def flush_notifications() -> None:
    from app.services import notifications

    with session_scope() as session, advisory_lock("flush_notifications") as ok:
        if ok:
            sent = notifications.flush(session)
            if sent:
                log.info("notifications sent: %d", sent, extra={"job": "flush"})


def robots_recheck() -> None:
    with session_scope() as session, advisory_lock("robots_recheck") as ok:
        if not ok:
            return
        results = robots.refresh_all(session)
        log.info("robots recheck: %s", results, extra={"job": "robots_recheck"})


def backup() -> Path | None:
    """pg_dump -Fc into settings.backup_dir, keep the newest settings.backup_keep files."""
    url = make_url(settings.database_url)
    target_dir = Path(settings.backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = target_dir / f"imoveis-{stamp}.dump"
    cmd = ["pg_dump", "-Fc", "-h", url.host or "localhost", "-p", str(url.port or 5432), "-U", url.username or "", "-d", url.database or "", "-f", str(target)]
    env = {**os.environ, "PGPASSWORD": url.password or ""}
    try:
        subprocess.run(cmd, check=True, env=env, capture_output=True, text=True, timeout=1800)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        log.error("backup failed: %s", detail, extra={"job": "backup"})
        return None
    dumps = sorted(target_dir.glob("imoveis-*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in dumps[settings.backup_keep:]:
        old.unlink(missing_ok=True)
    log.info("backup written %s (%d bytes)", target.name, target.stat().st_size, extra={"job": "backup"})
    return target


# --- agency discovery (Day 5) ------------------------------------------------------------------
# Four paced jobs instead of one per agency: with hundreds of agency sources, one APScheduler job each
# would mean hundreds of timers. `enabled_portal_schedules()` already filters `kind == 'portal'`, so the
# agency rows never reach the per-source scheduling at all.

def agency_directory() -> None:
    """Re-read the Imovirtual agency directory for her 50 concelhos (142 pages, ~5 min). Weekly: new
    agencies appear, and `adsSell` moving above zero is what re-opens a previously dead account."""
    from app.services import agencies

    with session_scope() as session, advisory_lock("agency_directory") as ok:
        if not ok:
            return
        stats = agencies.crawl_directory(session)
        log.info("agency directory: %s", stats, extra={"job": "agency_directory"})


def agency_websites() -> None:
    """Resolve agency websites from their Imovirtual profile pages, in bounded bites."""
    from app.services import agencies

    with session_scope() as session, advisory_lock("agency_websites") as ok:
        if not ok:
            return
        stats = agencies.resolve_websites(session, limit=60)
        if stats["checked"]:
            log.info("agency websites: %s", stats, extra={"job": "agency_websites"})


def agency_profile() -> None:
    """Profile the next few unprofiled agency sites: platform, listing pattern, measured confidence."""
    from app.services import agencies

    with session_scope() as session, advisory_lock("agency_profile") as ok:
        if not ok:
            return
        done = 0
        for agency in agencies.pending_profiles(session, limit=8):
            agencies.profile_agency(session, agency)
            session.commit()
            done += 1
        if done:
            log.info("agency profiles built: %d", done, extra={"job": "agency_profile"})


def agency_watch() -> None:
    """One pass over the agency sites that are due, oldest first, then refresh their off-portal ratios."""
    from app.services import agencies

    with session_scope() as session, advisory_lock("agency_watch") as ok:
        if not ok:
            return
        for source in agencies.due_for_watch(session, limit=40):
            first_pass = source.last_ok_at is None
            agencies.watch(session, source, initial=first_pass)
            agencies.refresh_offportal(session, source)
            session.commit()
