"""Background scheduler: fires lottery draws when they're due and sends
time-based nudges (pre-draw review reminders, overdue progress).

Long-running deployments (polling mode, webhook server, gunicorn workers)
run `scheduler_loop` as an asyncio task, started from the webserver's
startup hook. It's safe to run multiple instances (several gunicorn
workers, app restarts): every draw is claimed atomically in MongoDB
before it runs, so exactly one worker executes it.

Serverless targets (Vercel) can't keep a background task alive; there,
`run_due_draws` is invoked lazily from incoming webhook updates and the
/wake endpoint — draw times may drift until the next request arrives, but
no draw can ever be skipped or double-run. An external cron service
pinging /wake gives serverless deployments timely draws.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from core import config
from core.i18n import t
from core.timeutils import format_draw_time
from db import repository as repo
from services import draw_service

logger = logging.getLogger("digital-equb.scheduler")


async def run_due_draws(bot) -> int:
    """Claim and run every due draw; returns how many draws executed.
    Also handles the time-based nudges. Cheap enough to call from anywhere
    (scheduler tick, webhook update, /wake)."""
    now = repo.utcnow()
    due = await repo.get_due_draw_groups(now)

    executed = 0
    for group in due:
        claimed = await repo.claim_due_draw(group["_id"], now)
        if not claimed:
            continue  # another worker got it
        try:
            result = await draw_service.run_draw(bot, claimed)
            if result.get("drawn"):
                executed += 1
        except Exception:
            logger.exception("Draw failed for group %s — will be retried", claimed["_id"])
            # Leave draw_state="drawing": the stale-claim window (10 min)
            # makes the scheduler retry automatically.

    await _send_pre_draw_reminders(bot, now)
    await _notify_overdue_progress(bot)
    return executed


async def _send_pre_draw_reminders(bot, now) -> None:
    """~N minutes before each draw, remind admins about proofs that are
    still awaiting review — a member whose proof isn't approved by the draw
    time is NOT in the draw."""
    window_end = now + timedelta(minutes=config.ADMIN_PRE_DRAW_REMIND_MINUTES)
    groups = await repo.get_pre_draw_reminder_groups(now, window_end)
    for group in groups:
        awaiting = await repo.get_awaiting_review_contributions(group["_id"], group.get("cycle_number"))
        if not awaiting:
            continue  # leave the flag unset; remind later if proofs appear
        await repo.mark_pre_draw_reminder_sent(group["_id"])
        lang = await repo.get_chat_language(group["chat_id"])
        text = t(
            lang,
            "pre_draw_review_reminder",
            name=group["name"],
            period=group["current_period"],
            count=len(awaiting),
            when=format_draw_time(group["draw_at"]),
        )
        for admin_id in await repo.get_admins():
            try:
                await bot.send_message(admin_id, text)
            except Exception:
                pass


async def _notify_overdue_progress(bot) -> None:
    """Overdue rounds (draw fired with zero payers) where someone has NOW
    paid: nudge admins once so they can /drawnow or /postpone."""
    for group in await repo.get_overdue_groups():
        if group.get("overdue_eligible_notified"):
            continue
        try:
            await draw_service.notify_overdue_progress(bot, group)
        except Exception:
            logger.exception("Overdue-progress notification failed for group %s", group["_id"])


async def scheduler_loop(bot) -> None:
    logger.info(
        "Scheduler started (interval: %ss) — draws fire automatically at their scheduled time.",
        config.SCHEDULER_INTERVAL_SECONDS,
    )
    while True:
        try:
            await run_due_draws(bot)
        except Exception:
            logger.exception("Scheduler tick failed")
        await asyncio.sleep(config.SCHEDULER_INTERVAL_SECONDS)


def start_scheduler(bot) -> asyncio.Task:
    return asyncio.create_task(scheduler_loop(bot))
