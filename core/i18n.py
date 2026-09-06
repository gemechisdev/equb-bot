"""Multi-language support for user-facing text.

All strings live in langs/<code>.yml, one flat key -> string mapping per
language. English (langs/en.yml) is loaded first and used both as the
default language and as the fallback for any key missing from another
language file, so a partial translation never breaks the bot.

Usage:

    from core.i18n import t
    await message.answer(t(lang, "welcome"))
    await message.answer(t(lang, "number_reserved_for_you", number=5))

Language is resolved per-chat (see db/repository.py get_chat_language /
set_chat_language) so a group chat and a private chat can each have their
own language, independent of one another.
"""

from __future__ import annotations

import logging
import os

import yaml

logger = logging.getLogger("digital-equb.i18n")

_LANGS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "langs")

DEFAULT_LANG = "en"
SUPPORTED_LANGS = ["en", "am", "om"]

LANGUAGE_NAME_KEYS = {
    "en": "language_name_en",
    "am": "language_name_am",
    "om": "language_name_om",
}

_translations: dict[str, dict[str, str]] = {}


def _load_all():
    for lang in SUPPORTED_LANGS:
        path = os.path.join(_LANGS_DIR, f"{lang}.yml")
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            logger.error("Missing language file: %s", path)
            data = {}
        _translations[lang] = data


_load_all()


def normalize_lang(lang: str | None) -> str:
    """Returns `lang` if it's a supported language code, otherwise the
    default language. Safe to call with None / garbage input (e.g. a
    corrupted DB value)."""
    return lang if lang in SUPPORTED_LANGS else DEFAULT_LANG


def t(lang: str | None, key: str, **kwargs) -> str:
    """Look up `key` in the given language, falling back to English, and
    finally to the key itself if it's missing everywhere (so a typo or a
    not-yet-translated string is visible instead of raising)."""
    lang = normalize_lang(lang)
    template = _translations.get(lang, {}).get(key)
    if template is None and lang != DEFAULT_LANG:
        template = _translations.get(DEFAULT_LANG, {}).get(key)
    if template is None:
        logger.warning("Missing translation key: %s", key)
        return key
    try:
        return template.format(**kwargs)
    except Exception:
        logger.exception("Failed to format translation key: %s", key)
        return template


def language_display_name(lang: str, in_lang: str | None = None) -> str:
    """Human-readable name of `lang`, rendered using `in_lang`'s own
    strings (defaults to `lang` itself, e.g. Amharic name shown in Amharic)."""
    key = LANGUAGE_NAME_KEYS.get(lang, LANGUAGE_NAME_KEYS[DEFAULT_LANG])
    return t(in_lang or lang, key)
