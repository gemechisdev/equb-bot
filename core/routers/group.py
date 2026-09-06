from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from core.i18n import t
from core.pinning import announce_and_pin
from core.texts import (
    build_cycle_started_text,
    build_group_summary_line,
    build_members_text,
    build_status_text,
    format_user_identity,
)
from core.timeutils import format_draw_time
from db import repository as repo
from services import equb_service
from services.equb_service import EqubError
from services.user_identity import resolve_user_identity

router = Router(name="group")


async def _is_admin(user_id: int) -> bool:
    return await repo.is_user_admin(user_id)


def _sender_identity(message: Message) -> dict:
    u = message.from_user
    return {"telegram_id": u.id, "username": u.username, "display_name": u.full_name}


def _error_text(lang: str, error: EqubError) -> str:
    code = error.args[0]
    # Usage errors map to their dedicated usage strings; the rest are
    # "error_<code>" keys.
    if code.startswith("usage_"):
        return t(lang, code)
    return t(lang, f"error_{code}")


async def _require_group_chat(message: Message, lang: str) -> bool:
    if message.chat.type == "private":
        await message.answer(t(lang, "group_commands_require_group_chat"))
        return False
    return True


async def _resolve_member_target(message: Message, token: str | None) -> tuple[dict | None, str | None]:
    """Resolve '/addmember|/removemember <target>' to a Telegram identity.
    Accepts a reply to the member's message, a numeric user id, or an
    @username (resolved from the users cache the bot builds from every
    message it sees — the Bot API has no username->id lookup).
    Returns (identity, error_key)."""
    reply = message.reply_to_message
    if reply and reply.from_user and not reply.from_user.is_bot:
        u = reply.from_user
        return {"telegram_id": u.id, "username": u.username, "display_name": u.full_name}, None

    if not token:
        return None, "usage_addmember"

    token = token.strip()
    if token.lstrip("@").isdigit():
        identity = await resolve_user_identity(
            message.bot, message.chat.id, telegram_id=int(token.lstrip("@"))
        )
        return identity, None

    cached = await repo.find_user_by_username(token)
    if not cached:
        return None, "addmember_not_found"

    identity = await resolve_user_identity(
        message.bot, message.chat.id,
        telegram_id=cached["telegram_id"], username=cached.get("username"),
        display_name=cached.get("full_name"),
    )
    return identity, None


@router.message(Command("newequb", "ne"))
async def cmd_newequb(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    if not await _require_group_chat(message, lang):
        return
    if not await _is_admin(message.from_user.id):
        return

    raw = message.text.split(maxsplit=1)
    if len(raw) < 2:
        await message.answer(t(lang, "usage_newequb"))
        return

    try:
        args = equb_service.parse_newequb_args(raw[1])
    except EqubError as e:
        await message.answer(_error_text(lang, e))
        return

    try:
        group = await equb_service.create_group(
            chat_id=message.chat.id,
            name=args["name"],
            amount=args["amount"],
            currency=args["currency"],
            frequency=args["frequency"],
            interval_days=args["interval_days"],
            restart_mode=args["restart_mode"],
            creator=_sender_identity(message),
        )
    except EqubError as e:
        await message.answer(_error_text(lang, e))
        return

    text = t(lang, "group_created", name=group["name"], number=group["group_number"],
             amount=args["amount"], currency=args["currency"])
    if args["restart_mode"] == "auto":
        text += "\n\n" + t(lang, "group_created_auto")
    await message.answer(text)


@router.message(Command("joinequb", "join"))
async def cmd_joinequb(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    if not await _require_group_chat(message, lang):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group:
        await message.answer(t(lang, "no_open_group"))
        return

    identity = await resolve_user_identity(
        message.bot, message.chat.id, telegram_id=message.from_user.id,
        username=message.from_user.username, display_name=message.from_user.full_name,
    )

    try:
        await equb_service.join_group(group, identity)
    except EqubError as e:
        await message.answer(_error_text(lang, e))
        return

    count = await repo.count_active_members(group["_id"])
    await message.answer(t(lang, "joined_group", name=group["name"], count=count))


@router.message(Command("addmember", "am"))
async def cmd_addmember(message: Message):
    """Admin adds a specific person by @username, user id, or reply."""
    lang = await repo.get_chat_language(message.chat.id)
    if not await _require_group_chat(message, lang):
        return
    if not await _is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    target, error_key = await _resolve_member_target(message, parts[1].strip() if len(parts) > 1 else None)
    if not target:
        await message.answer(t(lang, error_key, token=parts[1].strip() if len(parts) > 1 else ""))
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group:
        await message.answer(t(lang, "no_open_group"))
        return

    try:
        result = await equb_service.admin_add_member(group, target)
    except EqubError as e:
        await message.answer(_error_text(lang, e))
        return

    who = format_user_identity(target.get("display_name"), target.get("username"), target["telegram_id"])
    key = "member_added_next_round" if result["next_round"] else "member_added"
    await message.answer(t(lang, key, who=who, name=group["name"]))


@router.message(Command("leaveequb", "leave"))
async def cmd_leaveequb(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    if not await _require_group_chat(message, lang):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group:
        await message.answer(t(lang, "no_open_group"))
        return

    try:
        await equb_service.leave_or_remove_member(group, message.from_user.id)
    except EqubError as e:
        await message.answer(_error_text(lang, e))
        return

    await message.answer(t(lang, "left_group", name=group["name"]))


@router.message(Command("removemember", "rmmember"))
async def cmd_removemember(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    if not await _is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    target, error_key = await _resolve_member_target(message, parts[1].strip() if len(parts) > 1 else None)
    if not target:
        await message.answer(t(lang, "usage_removemember" if error_key == "usage_addmember" else error_key,
                               token=parts[1].strip() if len(parts) > 1 else ""))
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group:
        await message.answer(t(lang, "no_open_group"))
        return

    try:
        await equb_service.leave_or_remove_member(group, target["telegram_id"])
    except EqubError as e:
        await message.answer(_error_text(lang, e))
        return

    await message.answer(t(lang, "member_removed", name=group["name"]))


@router.message(Command("members", "listmembers"))
async def cmd_members(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    group = await repo.get_active_or_open_group(message.chat.id)
    if not group:
        await message.answer(t(lang, "no_open_group"))
        return

    members = await repo.list_members(group["_id"], active_only=True)
    await message.answer(build_members_text(group, members, lang))


@router.message(Command("startcycle", "sc"))
async def cmd_startcycle(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    if not await _is_admin(message.from_user.id):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group:
        await message.answer(t(lang, "no_open_group"))
        return

    try:
        result = await equb_service.start_cycle(group)
    except EqubError as e:
        await message.answer(_error_text(lang, e))
        return

    group = await repo.get_group(group["_id"])
    text = build_cycle_started_text(group, len(result["members"]), lang, result["seed_hash"], result["draw_at"])
    await announce_and_pin(message.bot, message.chat.id, group["_id"], text)

    payment_methods = await repo.list_payment_methods(active_only=True)
    footer = ""
    if payment_methods:
        lines = [t(lang, "payment_methods_header")]
        for pm in payment_methods:
            lines.append(f"• {pm['name']}: {pm['details']}")
        footer = "\n\n" + "\n".join(lines)

    when = format_draw_time(result["draw_at"])
    for m in result["members"]:
        try:
            await message.bot.send_message(
                m["telegram_id"],
                t(lang, "dm_cycle_started", group_name=group["name"], amount=group["contribution_amount"],
                  currency=group["currency"], when=when) + footer,
            )
        except Exception:
            pass


@router.message(Command("status", "board"))
async def cmd_status(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    group = await repo.get_active_or_open_group(message.chat.id)
    if not group:
        await message.answer(t(lang, "no_open_group"))
        return

    members = await repo.list_members(group["_id"], active_only=True)
    contributions, payout, payouts = [], None, []
    if group["status"] == "active":
        contributions = await repo.get_contributions_for_period(
            group["_id"], group["current_period"], group.get("cycle_number")
        )
        payout = await repo.get_payout(group["_id"], group["current_period"], group.get("cycle_number"))
        payouts = await repo.list_payouts(group["_id"])

    await message.answer(build_status_text(group, members, contributions, payout, lang, payouts))


@router.message(Command("cancelequb",))
async def cmd_cancelequb(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    if not await _is_admin(message.from_user.id):
        return

    group = await repo.get_active_or_open_group(message.chat.id)
    if not group:
        await message.answer(t(lang, "no_open_group"))
        return

    try:
        await equb_service.cancel_group(group)
    except EqubError as e:
        await message.answer(_error_text(lang, e))
        return

    await message.answer(t(lang, "group_cancelled", name=group["name"]))


@router.message(Command("listequbs", "equbs"))
async def cmd_listequbs(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    groups = await repo.list_groups(message.chat.id)
    if not groups:
        await message.answer(t(lang, "no_groups_yet"))
        return

    lines = [t(lang, "groups_list_title")]
    for g in groups:
        lines.append(build_group_summary_line(g, lang))
    await message.answer("\n".join(lines))
