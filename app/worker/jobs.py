"""Scheduled jobs. Every job: open a session, take the advisory lock, do idempotent work, log one line.
Source sweeps are registered here from Day 2 (imovirtual_sweep, remax_incremental, ...)."""
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

    with session_scope() as session, advisory_lock(session, "run_queued") as ok:
        if not ok:
            return
        done = runs.run_queued(session)
        if done:
            log.info("queued runs executed: %d", done, extra={"job": "run_queued"})


def sweep_source(source_key: str) -> None:
    """Incremental sweep of one portal, then matching, events and Telegram flush."""
    from app.services import runs

    with session_scope() as session, advisory_lock(session, f"sweep:{source_key}") as ok:
        if not ok:
            log.info("sweep already running, skipped", extra={"job": "sweep", "source": source_key})
            return
        runs.execute_sweep(session, source_key)


def flush_notifications() -> None:
    from app.services import notifications

    with session_scope() as session, advisory_lock(session, "flush_notifications") as ok:
        if ok:
            sent = notifications.flush(session)
            if sent:
                log.info("notifications sent: %d", sent, extra={"job": "flush"})


def robots_recheck() -> None:
    with session_scope() as session, advisory_lock(session, "robots_recheck") as ok:
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
