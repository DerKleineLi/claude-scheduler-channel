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
import uuid
from pathlib import Path
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
import mcp.types as types

# ---------------------------------------------------------------------------
# Logging — stderr only. stdout is the MCP transport.
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s scheduler: %(message)s",
)
log = logging.getLogger("scheduler")

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
    """Register persisted jobs with APScheduler on startup."""
    for job in load_jobs():
        try:
            scheduler.add_job(
                fire_prompt,
                CronTrigger.from_crontab(normalize_cron(job["cron"])),
                args=[job["prompt"], job["id"], job["name"]],
                id=job["id"],
                replace_existing=True,
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


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        scheduler.start()
        schedule_existing_jobs()
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
