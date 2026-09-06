from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, Message

from core.i18n import SUPPORTED_LANGS, t
from core.keyboards import build_language_kb
from core.routers.contribution import send_contribute_instructions
from db import repository as repo

router = Router(name="common")


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    lang = await repo.get_chat_language(message.chat.id)

    # Deep link from the pinned Contribute button (t.me/<bot>?start=contribute):
    # opens the bot's DM — starting it first if necessary — and immediately
    # asks for the payment proof instead of showing the generic welcome.
    if command.args == "contribute" and message.chat.type == "private":
        await send_contribute_instructions(message.bot, message.chat.id, lang, message.from_user.id)
        return

    await message.answer(t(lang, "start_welcome"))


@router.message(Command("help"))
async def cmd_help(message: Message):
    lang = await repo.get_chat_language(message.chat.id)
    await message.answer(t(lang, "help_text"))


@router.message(Command("language", "lang"))
async def cmd_language(message: Message):
    lang = await repo.get_chat_language(message.chat.id)

    if message.chat.type != "private":
        if not await repo.is_user_admin(message.from_user.id):
            await message.answer(t(lang, "only_admin_can_change_language"))
            return

    await message.answer(t(lang, "choose_language"), reply_markup=build_language_kb())


@router.callback_query(F.data.startswith("setlang:"))
async def cb_set_language(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]
    if code not in SUPPORTED_LANGS:
        await callback.answer()
        return

    if callback.message.chat.type != "private":
        if not await repo.is_user_admin(callback.from_user.id):
            await callback.answer(t(await repo.get_chat_language(callback.message.chat.id), "only_admin_can_change_language"), show_alert=True)
            return

    await repo.set_chat_language(callback.message.chat.id, code)
    await callback.message.edit_text(t(code, "language_set_confirmation"))
    await callback.answer()
