"""Inline keyboards: quality picker, cancel, and retry suggestions.

Callback data is kept short — Telegram allows 64 bytes — and always carries a
short token for the probe result so a stale button cannot start a wrong download.
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.qualities import QualityOption

MAX_CALLBACK_BYTES = 64


def _check(data: str) -> str:
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback_data too long: {data}")
    return data


def quality_keyboard(options: list[QualityOption], token: str) -> InlineKeyboardMarkup:
    """One button per available quality, two per row, plus a cancel button."""
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []

    for option in options:
        current.append(InlineKeyboardButton(option.label, callback_data=_check(f"dl:{option.key}:{token}")))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)

    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=_check(f"abort:{token}"))])
    return InlineKeyboardMarkup(rows)


def retry_keyboard(options: list[QualityOption], token: str, limit: int = 3) -> InlineKeyboardMarkup:
    """Smaller qualities to try after a failure (usually "too large")."""
    chosen = [option for option in options if option.kind == "video"][:limit]
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for option in chosen:
        current.append(InlineKeyboardButton(f"🔁 {option.label}", callback_data=_check(f"dl:{option.key}:{token}")))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    if not rows:
        return InlineKeyboardMarkup([])
    return InlineKeyboardMarkup(rows)


def audio_retry_keyboard(options: list[QualityOption], token: str) -> InlineKeyboardMarkup | None:
    for option in options:
        if option.is_audio:
            return InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"🎵 {option.label}", callback_data=_check(f"dl:{option.key}:{token}"))]]
            )
    return None


def cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Shown on the live status message so a run can be stopped with one tap."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel download", callback_data=_check(f"cancel:{job_id}"))]]
    )
