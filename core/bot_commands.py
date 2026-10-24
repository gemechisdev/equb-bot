"""Telegram BotCommand menu registration.

Commands shown when the user opens the "/" menu in Telegram. Descriptions
must stay short (Telegram limit ~256 chars). Admin-only commands are marked
with [admin] so members know they won't work for them.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, BotCommandScopeDefault

logger = logging.getLogger("digital-equb.bot_commands")

# (command, description) — primary names only (aliases still work in chat).
# Keep descriptions under ~50 chars for clean mobile menus.
_MEMBER_COMMANDS: list[tuple[str, str]] = [
    ("start", "Open the bot / deep-link actions"),
    ("help", "Full command list and how Equb works"),
    ("language", "Set language (English / Amharic / Afaan Oromoo)"),
    ("contribute", "Submit payment proof (private chat)"),
    ("joinequb", "Join the Equb in this group"),
    ("leaveequb", "Leave before the cycle starts"),
    ("members", "List current members"),
    ("status", "Next draw, who paid, who already won"),
    ("recipient", "Who won this round (or when draw is due)"),
    ("history", "Payout history for this group"),
    ("verify", "Re-check a draw's fairness math"),
    ("statement", "Cycle statement (add csv to export)"),
    ("listequbs", "List Equbs created in this chat"),
]

_ADMIN_COMMANDS: list[tuple[str, str]] = [
    ("newequb", "[admin] Create Equb: Name|amount|freq|[cur]|[auto]"),
    ("startcycle", "[admin] Lock members and open round 1"),
    ("addmember", "[admin] Add a member by @user, id, or reply"),
    ("removemember", "[admin] Remove a member"),
    ("pending", "[admin] Review payment proofs"),
    ("drawnow", "[admin] Draw winner now among verified payers"),
    ("postpone", "[admin] Move the draw (e.g. /postpone 24)"),
    ("payout", "[admin] Confirm winner was paid (advances cycle)"),
    ("remind", "[admin] DM members who have not paid yet"),
    ("unverify", "[admin] Undo a verification before the draw"),
    ("reconcile", "[admin] Fix stuck paid-but-not-advanced cycle"),
    ("cancelequb", "[admin] Cancel this Equb (needs confirm)"),
    ("auditlog", "[admin] Recent financial/admin events"),
    ("addadmin", "[admin] Grant bot admin by telegram id"),
    ("deladmin", "[admin] Revoke bot admin"),
    ("listadmins", "[admin] List bot admins"),
    ("addpayment", "[admin] Add payment method: Name | Details"),
    ("editpayment", "[admin] Edit payment method"),
    ("delpayment", "[admin] Delete payment method"),
    ("togglepayment", "[admin] Enable/disable a payment method"),
    ("listpayments", "[admin] List payment methods"),
    ("chat", "[admin] Message a user by telegram id"),
]


def all_bot_commands() -> list[BotCommand]:
    """Full menu: member commands first, then admin."""
    items = _MEMBER_COMMANDS + _ADMIN_COMMANDS
    return [BotCommand(command=c, description=d) for c, d in items]


def member_bot_commands() -> list[BotCommand]:
    return [BotCommand(command=c, description=d) for c, d in _MEMBER_COMMANDS]


async def register_bot_commands(bot: Bot) -> None:
    """Push the command menu to Telegram (default + private + groups)."""
    commands = all_bot_commands()
    try:
        await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
        await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
        await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
        logger.info("Registered %d bot commands with Telegram", len(commands))
    except Exception:
        logger.exception("Failed to register bot commands")
