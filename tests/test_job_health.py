"""The worker must not be able to fail quietly (QA items 1.4, 4.5, 5.2, 5.3, 5.6).

Measured 2026-11-04 against a real APScheduler: a job that raises keeps being rescheduled, the scheduler
stays up, other jobs are unaffected, and the only trace is one library log line. Ten of the seventeen jobs
create no `crawl_runs` row, so nothing recorded them at all. Two of those ten are `backup` (we would find
out when a restore is needed) and `flush_notifications` (the path that silently destroyed 96 of her alerts
on 2026-11-04).

Driven with a stub scheduler and a dict-backed settings store, in the style of test_schedule_reload.py.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import jobhealth

SESSION = object()
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


class _Store:
    """The app_settings row, as a dict, counting writes."""

    def __init__(self):
        self.values = {}
        self.writes = 0

    def get(self, _session, key, default=None):
        return self.values.get(key, default)

    def put(self, _session, key, value):
        self.values[key] = value
        self.writes += 1


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(jobhealth.appsettings, "get", s.get)
    monkeypatch.setattr(jobhealth.appsettings, "put", s.put)
    return s


def _job(job_id, next_run, interval_s=None):
    trigger = SimpleNamespace(interval=timedelta(seconds=interval_s)) if interval_s else SimpleNamespace()
    return SimpleNamespace(id=job_id, next_run_time=next_run, trigger=trigger)


# --- recording ----------------------------------------------------------------------------------

def test_a_failure_is_counted_and_its_reason_kept(store):
    assert jobhealth.record_failure(SESSION, "backup", "pg_dump exited 1") == 1
    assert jobhealth.record_failure(SESSION, "backup", "pg_dump exited 1") == 2
    entry = jobhealth.read(SESSION)["backup"]
    assert entry["fails"] == 2 and "pg_dump" in entry["error"] and entry["last_fail"]


def test_a_success_clears_the_entry_so_an_empty_dict_means_healthy(store):
    jobhealth.record_failure(SESSION, "backup", "boom")
    assert jobhealth.record_success(SESSION, "backup") is True
    assert jobhealth.read(SESSION) == {}


def test_a_success_for_a_healthy_job_writes_nothing(store):
    """`run_queued` fires every 30 seconds. Recording every success would be thousands of writes a day
    for no information, and the dict must stay bounded by the number of BROKEN jobs."""
    before = store.writes
    assert jobhealth.record_success(SESSION, "run_queued") is False
    assert store.writes == before, "a healthy tick must not write"


def test_only_broken_jobs_are_stored(store):
    jobhealth.record_failure(SESSION, "a", "x")
    jobhealth.record_failure(SESSION, "b", "y")
    jobhealth.record_success(SESSION, "a")
    assert list(jobhealth.read(SESSION)) == ["b"]


def test_the_error_text_is_capped(store):
    jobhealth.record_failure(SESSION, "a", "E" * 5000)
    assert len(jobhealth.read(SESSION)["a"]["error"]) <= 500


# --- the threshold ------------------------------------------------------------------------------

def test_one_failure_is_a_blip_two_is_a_problem():
    assert jobhealth.failing({"a": {"fails": 1, "error": "x"}}) == []
    assert jobhealth.failing({"a": {"fails": 2, "error": "x"}}) == [("a", 2, "x")]


def test_the_threshold_is_the_same_one_source_health_uses():
    """Two definitions of "not a blip" would drift apart."""
    from app.services import health

    assert jobhealth.FAILS_BEFORE_DEGRADED == health.FAILURES_BEFORE_DEGRADED


def test_the_worst_offender_is_listed_first():
    state = {"a": {"fails": 2, "error": ""}, "b": {"fails": 9, "error": ""}}
    assert [r[0] for r in jobhealth.failing(state)] == ["b", "a"]


# --- overdue ------------------------------------------------------------------------------------

def test_a_paused_job_is_never_overdue():
    """[!] The trap: a paused job reports `next_run_time = None` (verified against a real scheduler).
    Treating that as infinitely late would report every paused job as broken."""
    assert jobhealth.overdue([_job("paused", None, 120)], now=NOW) == []


def test_a_job_due_in_the_future_is_not_overdue():
    assert jobhealth.overdue([_job("fine", NOW + timedelta(minutes=5), 120)], now=NOW) == []


def test_a_frequent_job_running_far_late_is_overdue():
    late = jobhealth.overdue([_job("flush_notifications", NOW - timedelta(minutes=40), 120)], now=NOW)
    assert late and late[0][0] == "flush_notifications"
    assert late[0][1] == pytest.approx(2400, abs=1)


def test_an_infrequent_job_is_judged_against_its_own_interval():
    """A weekly job 20 minutes late is fine; the same lateness on a 2-minute job is not. Without this,
    every infrequent job would be reported the moment it drifted."""
    weekly = _job("agency_directory", NOW - timedelta(minutes=20), 7 * 24 * 3600)
    frequent = _job("run_queued", NOW - timedelta(minutes=20), 30)
    assert jobhealth.overdue([weekly], now=NOW) == []
    assert [j for j, _ in jobhealth.overdue([frequent], now=NOW)] == ["run_queued"]


def test_the_floor_is_above_the_schedulers_own_misfire_grace():
    """`misfire_grace_time` is 600 s, so a job may legitimately be that late. A floor below it would
    report normal behaviour as a fault."""
    assert jobhealth.OVERDUE_FLOOR_S > 600
    cron_ish = _job("backup", NOW - timedelta(minutes=9))  # no interval, 9 minutes late
    assert jobhealth.overdue([cron_ish], now=NOW) == []


# --- the verdict reaches the panel --------------------------------------------------------------

class _EvalSession:
    def __init__(self, worker_row, stalled=()):
        self.worker, self._stalled = worker_row, stalled

    def scalar(self, _stmt):
        return self.worker

    def execute(self, _stmt):
        return SimpleNamespace(all=lambda: list(self._stalled))


def _worker_row():
    return SimpleNamespace(key="worker", name="Tarefas automáticas", kind="worker", status="ok",
                           last_ok_at=None, last_error=None, consecutive_failures=0)


def test_a_repeatedly_failing_job_degrades_the_worker_row(store):
    jobhealth.record_failure(SESSION, "backup", "pg_dump exited 1")
    jobhealth.record_failure(SESSION, "backup", "pg_dump exited 1")
    worker = _worker_row()
    problems = jobhealth.evaluate(_EvalSession(worker), [], now=NOW)
    assert worker.status == "degraded"
    assert any("backup" in p for p in problems)
    assert "pg_dump" in (worker.last_error or "")


def test_a_healthy_worker_recovers_the_row(store):
    worker = _worker_row()
    worker.status, worker.consecutive_failures = "degraded", 3
    assert jobhealth.evaluate(_EvalSession(worker), [], now=NOW) == []
    assert worker.status == "ok" and worker.consecutive_failures == 0


def test_a_run_stuck_mid_flight_is_reported(store):
    stuck = [(1, "remax", "backfill", NOW - timedelta(hours=9))]
    worker = _worker_row()
    problems = jobhealth.evaluate(_EvalSession(worker, stuck), [], now=NOW)
    assert any("remax" in p and "presa" in p for p in problems)
    assert worker.status == "degraded"


def test_a_run_that_is_merely_long_is_not_reported(store):
    """A backfill slice legitimately takes a while; only an abandoned run matters."""
    recent = [(1, "remax", "backfill", NOW - timedelta(hours=1))]
    worker = _worker_row()
    assert jobhealth.evaluate(_EvalSession(worker, recent), [], now=NOW) == []


def test_the_worker_is_shown_in_the_panel_but_never_sent_to_her_phone():
    from app.services import health

    assert "worker" in health.BANNER_KINDS, "she must be able to see it"
    assert "worker" not in health.ANNOUNCED_KINDS, "a worker fault is not hers to act on"


def test_the_banner_does_not_alarm_her_about_our_plumbing(monkeypatch):
    from app.services import health

    monkeypatch.setattr(health, "unhealthy", lambda session, kinds=None: [
        SimpleNamespace(name="Tarefas automáticas", kind="worker", status="degraded",
                        last_error="a tarefa 'backup' falhou 3x seguidas", last_ok_at=None)])

    class _FakeSession:
        def execute(self, _stmt):
            return SimpleNamespace(all=lambda: [])

    row = health.banner(_FakeSession())[0]
    assert "nada se perde" in row["text"]
    assert "backup" in row["error"], "the detail stays, for whoever maintains it"
    assert row["ok_label"] == "Última verificação correta"


# --- the listener -------------------------------------------------------------------------------

def test_the_listener_records_both_outcomes_and_never_raises(monkeypatch):
    """An exception here would come from the scheduler's own thread, and a failing job must not be able
    to trigger it."""
    from app.worker import main as worker_main

    calls = []
    monkeypatch.setattr(worker_main, "session_scope", lambda: contextlib.nullcontext(SESSION))
    monkeypatch.setattr(jobhealth, "record_failure", lambda _s, job_id, err: calls.append(("fail", job_id)))
    monkeypatch.setattr(jobhealth, "record_success", lambda _s, job_id: calls.append(("ok", job_id)))

    worker_main._on_job_event(SimpleNamespace(job_id="backup", exception=RuntimeError("boom")))
    worker_main._on_job_event(SimpleNamespace(job_id="backup", exception=None))
    assert calls == [("fail", "backup"), ("ok", "backup")]

    def explode(*_a, **_k):
        raise Exception("db down")

    monkeypatch.setattr(jobhealth, "record_failure", explode)
    worker_main._on_job_event(SimpleNamespace(job_id="x", exception=RuntimeError("boom")))  # must not raise


def test_the_listener_is_registered_for_both_events():
    import inspect

    from app.worker import main as worker_main

    source = inspect.getsource(worker_main.build_scheduler)
    assert "add_listener(_on_job_event" in source
    assert "EVENT_JOB_ERROR" in source and "EVENT_JOB_EXECUTED" in source


def test_heartbeat_still_works_without_a_scheduler():
    """It must stay callable by hand and from the CLI."""
    import inspect

    from app.worker import jobs

    assert inspect.signature(jobs.heartbeat).parameters["scheduler"].default is None
