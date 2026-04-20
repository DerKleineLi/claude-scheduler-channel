#!/usr/bin/env python3
"""
Scheduler channel for Claude Code.

An MCP channel server that pushes scheduled prompts into a running Claude Code
session via `notifications/claude/channel`. Also exposes tools so Claude can
add, list, and remove cron jobs from within the session.

Run via Claude Code with:
    claude --dangerously-load-development-channels server:scheduler \
           --channels plugin:telegram@claude-plugins-official

Jobs persist to ~/.claude/scheduler_channel/jobs.json across restarts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MISSED,
    EVENT_JOB_SUBMITTED,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
import mcp.types as types

# ---------------------------------------------------------------------------
# Logging — stderr (captured by Claude Code MCP log) + rotating file so we
# have persistent history across MCP restarts to diagnose missed-fire issues.
# stdout is the MCP transport; do not write to it.
# ---------------------------------------------------------------------------
LOG_DIR = Path.home() / ".claude" / "scheduler_channel"
LOG_FILE = LOG_DIR / "debug.log"
LOG_DIR.mkdir(parents=True, exist_ok=True)

_fmt = logging.Formatter("%(asctime)s %(levelname)s scheduler: %(message)s")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s scheduler: %(message)s")
log = logging.getLogger("scheduler")

# Rotating file handler: 1 MB × 3 files = ~3 MB cap.
_file_handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_fmt)
log.addHandler(_file_handler)
log.setLevel(logging.DEBUG)

# Also pipe APScheduler's own logger through the same file + stderr.
_ap_log = logging.getLogger("apscheduler")
_ap_log.setLevel(logging.INFO)
_ap_log.addHandler(_file_handler)

# ---------------------------------------------------------------------------
# Cron normalization
# ---------------------------------------------------------------------------
# APScheduler's CronTrigger.from_crontab has a long-standing quirk: it treats
# the day-of-week field with its internal Mon=0 convention instead of the
# Unix-cron Sun=0 convention. So `* * * * 5` matches Saturday, not Friday.
# Names (sun/mon/tue/…) are parsed correctly. We normalize numeric DoW to
# names before handing the string to APScheduler so users can write standard
# Unix cron and get the expected behavior.
_DOW_NAMES = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"]


def normalize_cron(cron: str) -> str:
    parts = cron.strip().split()
    if len(parts) != 5:
        return cron  # let APScheduler raise a clear error
    dow = parts[4]
    if any(c.isalpha() for c in dow) or dow in ("*", "?"):
        return cron  # already names or wildcard — leave alone

    def _one(tok: str) -> str:
        if "-" in tok:
            a, b = tok.split("-", 1)
            return f"{_DOW_NAMES[int(a) % 7]}-{_DOW_NAMES[int(b) % 7]}"
        return _DOW_NAMES[int(tok) % 7]

    try:
        parts[4] = ",".join(_one(t) for t in dow.split(","))
    except (ValueError, IndexError):
        return cron  # step expressions etc — let APScheduler handle
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------
JOBS_FILE = Path.home() / ".claude" / "scheduler_channel" / "jobs.json"


def load_jobs() -> list[dict[str, Any]]:
    if not JOBS_FILE.exists():
        return []
    try:
        return json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("failed to load jobs: %s", e)
        return []


def save_jobs(jobs: list[dict[str, Any]]) -> None:
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    JOBS_FILE.write_text(json.dumps(jobs, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# MCP server + scheduler
# ---------------------------------------------------------------------------
server: Server = Server("scheduler")
scheduler = AsyncIOScheduler()

# Populated on first request. APScheduler callbacks read this to push.
_session: Any = None


async def fire_prompt(prompt: str, job_id: str, name: str) -> None:
    """APScheduler callback: push the scheduled prompt into the session.

    ServerNotification is a RootModel restricted to known notification types,
    so for a custom method like ``notifications/claude/channel`` we bypass it
    and send a raw JSONRPCNotification through ``session.send_message``.
    """
    if _session is None:
        log.warning("no session captured; dropping fire for %s (%s)", name, job_id)
        return
    log.info("firing %s (%s)", name, job_id)
    try:
        raw = types.JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={
                "content": prompt,
                "meta": {
                    "source": "scheduler",
                    "job": name,
                    "id": job_id,
                },
            },
        )
        msg = SessionMessage(message=types.JSONRPCMessage(root=raw))
        await _session.send_message(msg)
    except Exception as e:
        log.exception("failed to push notification: %s", e)


def schedule_existing_jobs() -> None:
    """Register persisted jobs with APScheduler on startup.

    `misfire_grace_time=None` + `coalesce=True` make the job catch up after
    any event-loop pause (Windows sleep, WSL2 VM idle-suspend, process pause)
    instead of being silently skipped once the default 1-second grace expires.
    Missed fires during a long pause collapse to one run at wake-up time.
    """
    for job in load_jobs():
        try:
            scheduler.add_job(
                fire_prompt,
                CronTrigger.from_crontab(normalize_cron(job["cron"])),
                args=[job["prompt"], job["id"], job["name"]],
                id=job["id"],
                replace_existing=True,
                misfire_grace_time=None,
                coalesce=True,
            )
            log.info("loaded job %s (%s) cron=%r", job["name"], job["id"], job["cron"])
        except Exception as e:
            log.error("skipping malformed job %r: %s", job, e)


# ---------------------------------------------------------------------------
# Tools — Claude can manage jobs from within the session
# ---------------------------------------------------------------------------
@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="add_job",
            description=(
                "Schedule a recurring prompt via cron. The prompt is pushed into "
                "this Claude session each time the cron matches. Jobs persist "
                "across restarts. Use standard 5-field local-time cron (min hour "
                "dom mon dow), e.g. '0 18 * * 4' = every Thursday 18:00."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short human-readable name, shown in list_jobs.",
                    },
                    "cron": {
                        "type": "string",
                        "description": "Standard 5-field cron in local time.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The prompt to push into the session on each fire.",
                    },
                },
                "required": ["name", "cron", "prompt"],
            },
        ),
        types.Tool(
            name="list_jobs",
            description="List all scheduled jobs with id, cron, and prompt preview.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="remove_job",
            description="Remove a scheduled job by id.",
            inputSchema={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Job id from list_jobs."}
                },
                "required": ["id"],
            },
        ),
        types.Tool(
            name="fire_now",
            description="Fire a scheduled job immediately for testing, without waiting for its cron.",
            inputSchema={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        ),
        types.Tool(
            name="tail_log",
            description=(
                "Return the last N lines of ~/.claude/scheduler_channel/debug.log. "
                "Use for post-mortem on missed fires: look for catch-up warnings "
                "(monotonic/wallclock drift) and event.missed / event.executed lines."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "lines": {"type": "integer", "default": 200, "description": "Number of trailing lines."},
                },
            },
        ),
    ]


def _capture_session() -> None:
    """Capture the session ref so background cron firings can push."""
    global _session
    if _session is None:
        try:
            _session = server.request_context.session
            log.info("captured session ref")
        except Exception as e:
            log.warning("could not capture session: %s", e)


@server.call_tool()
async def call_tool(name: str, args: dict[str, Any]) -> list[types.TextContent]:
    _capture_session()
    jobs = load_jobs()

    if name == "add_job":
        try:
            CronTrigger.from_crontab(normalize_cron(args["cron"]))  # validate
        except Exception as e:
            return [types.TextContent(type="text", text=f"Invalid cron '{args['cron']}': {e}")]

        job_id = uuid.uuid4().hex[:8]
        job = {
            "id": job_id,
            "name": args["name"],
            "cron": args["cron"],
            "prompt": args["prompt"],
        }
        jobs.append(job)
        save_jobs(jobs)
        scheduler.add_job(
            fire_prompt,
            CronTrigger.from_crontab(normalize_cron(job["cron"])),
            args=[job["prompt"], job["id"], job["name"]],
            id=job["id"],
            replace_existing=True,
            misfire_grace_time=None,
            coalesce=True,
        )
        next_fire = scheduler.get_job(job_id).next_run_time
        return [
            types.TextContent(
                type="text",
                text=(
                    f"Scheduled '{job['name']}' (id={job_id}) cron='{job['cron']}'.\n"
                    f"Next fire: {next_fire}"
                ),
            )
        ]

    if name == "list_jobs":
        if not jobs:
            return [types.TextContent(type="text", text="No scheduled jobs.")]
        lines = []
        for j in jobs:
            sj = scheduler.get_job(j["id"])
            nxt = sj.next_run_time if sj else "(not scheduled)"
            preview = j["prompt"].replace("\n", " ")[:80]
            lines.append(
                f"id={j['id']}  name={j['name']}\n"
                f"  cron={j['cron']}  next={nxt}\n"
                f"  prompt={preview}"
            )
        return [types.TextContent(type="text", text="\n\n".join(lines))]

    if name == "remove_job":
        target = args["id"]
        new_jobs = [j for j in jobs if j["id"] != target]
        removed = len(jobs) - len(new_jobs)
        save_jobs(new_jobs)
        try:
            scheduler.remove_job(target)
        except Exception:
            pass
        return [
            types.TextContent(
                type="text",
                text=f"Removed {removed} job(s) with id={target}.",
            )
        ]

    if name == "fire_now":
        target = args["id"]
        job = next((j for j in jobs if j["id"] == target), None)
        if not job:
            return [types.TextContent(type="text", text=f"No job with id={target}.")]
        await fire_prompt(job["prompt"], job["id"], job["name"])
        return [types.TextContent(type="text", text=f"Fired '{job['name']}' now.")]

    if name == "tail_log":
        n = int(args.get("lines", 200))
        if not LOG_FILE.exists():
            return [types.TextContent(type="text", text=f"(no log at {LOG_FILE})")]
        try:
            with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-n:]
            return [types.TextContent(type="text", text="".join(lines) or "(empty)")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"tail_log error: {e}")]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
INSTRUCTIONS = (
    "You are connected to a scheduler channel.\n\n"
    "Scheduled prompts arrive as <channel source=\"scheduler\" job=\"<name>\" id=\"<id>\">. "
    "The body is a prompt the user previously scheduled — treat it as a user instruction "
    "and act on it, then (if appropriate) send a Telegram reply summarizing the result.\n\n"
    "Manage schedules with add_job, list_jobs, remove_job, and fire_now tools."
)


def _install_scheduler_event_listeners() -> None:
    """Turn every APScheduler job-lifecycle event into a debug log line so
    missing fires can be diagnosed after the fact from debug.log."""
    def _on_submit(ev):
        log.debug("event.submitted id=%s scheduled=%s", ev.job_id, ev.scheduled_run_time)

    def _on_executed(ev):
        log.info("event.executed id=%s scheduled=%s", ev.job_id, ev.scheduled_run_time)

    def _on_missed(ev):
        log.warning("event.missed id=%s scheduled=%s", ev.job_id, ev.scheduled_run_time)

    def _on_error(ev):
        log.error("event.error id=%s scheduled=%s exc=%s", ev.job_id, ev.scheduled_run_time, ev.exception)

    scheduler.add_listener(_on_submit, EVENT_JOB_SUBMITTED)
    scheduler.add_listener(_on_executed, EVENT_JOB_EXECUTED)
    scheduler.add_listener(_on_missed, EVENT_JOB_MISSED)
    scheduler.add_listener(_on_error, EVENT_JOB_ERROR)
    log.info("scheduler event listeners installed")


async def _wallclock_catchup_loop(interval_sec: int = 30, overdue_tol_sec: int = 5) -> None:
    """Every ``interval_sec``, force-fire any job whose ``next_run_time`` is
    more than ``overdue_tol_sec`` in the past.

    Why this exists: APScheduler's AsyncIOScheduler schedules its internal
    wake-up via ``loop.call_later(delta_seconds, ...)``, where the delta is
    computed in wallclock but the countdown uses asyncio's *monotonic* clock.
    If the WSL2 utility VM (or host) pauses for a while, monotonic time
    freezes but wallclock jumps forward on resume. When the call_later
    eventually fires, APScheduler then evaluates ``misfire_grace_time`` —
    which we've set to None, so it *would* fire. But if the pause exceeds
    the delta, the call_later never fires in monotonic time within the
    session's lifetime, and the job is silently skipped.

    This task runs independently on a short interval. Each tick it walks
    jobs and, if any ``next_run_time`` is in the past, reschedules it to
    "now + 1s" which forces APScheduler to fire it on the very next tick of
    its own loop. APScheduler then recomputes the next cron occurrence
    normally, so we don't double-fire.
    """
    prev_monotonic = time.monotonic()
    prev_wall = time.time()
    heartbeat_every = 10  # emit a heartbeat log every 10 ticks (~5 min) so long gaps are visible
    tick = 0
    while True:
        try:
            await asyncio.sleep(interval_sec)
            tick += 1
            now_m = time.monotonic()
            now_w = time.time()
            # Expected ~interval_sec between ticks; anything >2× is a pause.
            gap_m = now_m - prev_monotonic
            gap_w = now_w - prev_wall
            drift = gap_w - gap_m
            if abs(drift) > 2.0:
                log.warning(
                    "catch-up: clock drift detected between ticks — monotonic_gap=%.1fs wallclock_gap=%.1fs drift=%.1fs (likely VM/host pause)",
                    gap_m, gap_w, drift,
                )
            elif tick % heartbeat_every == 0:
                log.debug("catch-up: heartbeat tick=%d monotonic_gap=%.1fs wallclock_gap=%.1fs", tick, gap_m, gap_w)
            prev_monotonic = now_m
            prev_wall = now_w

            now = datetime.now(timezone.utc)
            overdue_found = 0
            for job in scheduler.get_jobs():
                nr = job.next_run_time
                if nr is None:
                    continue
                nr_utc = nr.astimezone(timezone.utc)
                if nr_utc < now - timedelta(seconds=overdue_tol_sec):
                    overdue_found += 1
                    log.warning(
                        "catch-up: job %s (%s) was due at %s (%.0fs ago); forcing fire",
                        job.name, job.id, nr, (now - nr_utc).total_seconds(),
                    )
                    try:
                        scheduler.modify_job(
                            job.id,
                            next_run_time=datetime.now(nr.tzinfo) + timedelta(seconds=1),
                        )
                    except Exception as e:
                        log.exception("catch-up modify_job failed for %s: %s", job.id, e)
            if overdue_found == 0 and tick % heartbeat_every == 0:
                # attach next-run summary to the heartbeat (helps diagnose "why didn't it fire tomorrow either")
                summary = [f"{j.name}->{j.next_run_time}" for j in scheduler.get_jobs()]
                log.debug("catch-up: next_runs=%s", summary)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.exception("wallclock catch-up loop error (continuing): %s", e)


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        log.info("scheduler mcp starting; debug.log=%s pid=%s", LOG_FILE, os.getpid())
        _install_scheduler_event_listeners()
        scheduler.start()
        schedule_existing_jobs()
        # Catch-up loop: rescues jobs whose next_run_time slid into the past
        # during a VM/host pause (APScheduler's monotonic-clock timer can't
        # recover from long pauses on its own).
        asyncio.create_task(_wallclock_catchup_loop())
        init_opts = server.create_initialization_options(
            notification_options=NotificationOptions(),
            experimental_capabilities={"claude/channel": {}},
        )
        init_opts.instructions = INSTRUCTIONS
        await server.run(read_stream, write_stream, init_opts)


def _cli() -> None:
    """Console-scripts entry point. See pyproject.toml."""
    asyncio.run(main())


if __name__ == "__main__":
    _cli()
