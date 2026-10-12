"""Inline keyboards: quality picker, job controls and navigation.

Buttons use Telegram's button styles (Bot API 9.4: ``primary`` blue, ``success`` green,
``danger`` red). Clients older than February 2026 ignore the field and draw the usual
unstyled button, so nothing breaks for anyone — the colours are a bonus, not a dependency.

Layout of the picker, top to bottom: the recommended quality (green), the rest of the real
heights in pairs (blue), audio (blue), and cancel (red).
"""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from bot.qualities import QualityOption

MAX_CALLBACK_BYTES = 64

STYLE_PRIMARY = "primary"
STYLE_SUCCESS = "success"
STYLE_DANGER = "danger"


def _check(data: str) -> str:
    if len(data.encode()) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback_data too long: {data}")
    return data


def _button(text: str, data: str, style: str | None = None) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=_check(data), style=style)


def quality_keyboard(
    options: list[QualityOption],
    token: str,
    *,
    recommended: QualityOption | None = None,
) -> InlineKeyboardMarkup:
    """One button per real quality, the recommended one on its own green row."""
    rows: list[list[InlineKeyboardButton]] = []
    remaining = list(options)

    if recommended is not None and recommended in remaining:
        rows.append([_button(f"⭐️ {recommended.label.lstrip('⭐️ ')}", f"dl:{recommended.key}:{token}", STYLE_SUCCESS)])
        remaining.remove(recommended)

    video = [option for option in remaining if not option.is_audio]
    audio = [option for option in remaining if option.is_audio]

    current: list[InlineKeyboardButton] = []
    for option in video:
        current.append(_button(option.label, f"dl:{option.key}:{token}", STYLE_PRIMARY))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)

    for option in audio:
        rows.append([_button(option.label, f"dl:{option.key}:{token}", STYLE_PRIMARY)])

    rows.append([_button("❌ Cancel", f"abort:{token}", STYLE_DANGER)])
    return InlineKeyboardMarkup(rows)


def retry_keyboard(options: list[QualityOption], token: str, limit: int = 4) -> InlineKeyboardMarkup:
    """Smaller qualities to try after a failure — usually "too large"."""
    rows: list[list[InlineKeyboardButton]] = []
    current: list[InlineKeyboardButton] = []
    for option in options[:limit]:
        current.append(_button(f"🔁 {option.label}", f"dl:{option.key}:{token}", STYLE_PRIMARY))
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    return InlineKeyboardMarkup(rows) if rows else InlineKeyboardMarkup([])


def cancel_keyboard(job_id: str) -> InlineKeyboardMarkup:
    """Shown on the live status message so a run can be stopped with one tap."""
    return InlineKeyboardMarkup([[_button("❌ Cancel download", f"cancel:{job_id}", STYLE_DANGER)]])


def welcome_keyboard(bot_username: str = "", owner_username: str = "") -> InlineKeyboardMarkup:
    """The /start screen's shortcuts, plus the two things a public bot always needs."""
    rows = [
        [
            _button("📖 How to use", "nav:help", STYLE_PRIMARY),
            _button("📊 Status", "nav:status", STYLE_PRIMARY),
        ],
        [
            _button("📦 Size limits", "nav:limits", STYLE_PRIMARY),
            _button("ℹ️ About", "nav:about", STYLE_PRIMARY),
        ],
    ]
    links = []
    if bot_username:
        links.append(
            InlineKeyboardButton(
                "➕ Add to a group",
                url=f"https://t.me/{bot_username}?startgroup=true",
                style=STYLE_SUCCESS,
            )
        )
    if owner_username:
        links.append(
            InlineKeyboardButton("👤 Owner", url=f"https://t.me/{owner_username}", style=STYLE_PRIMARY)
        )
    if links:
        rows.append(links)
    return InlineKeyboardMarkup(rows)


def diagnostics_keyboard() -> InlineKeyboardMarkup:
    """The operator's refresh button — the only place /diag is reachable from."""
    return InlineKeyboardMarkup([[_button("🔄 Refresh", "nav:diag", STYLE_PRIMARY)]])


def delivered_keyboard(source_url: str) -> InlineKeyboardMarkup:
    """Attached under a delivered file: a way back to the source."""
    if not source_url:
        return InlineKeyboardMarkup([])
    return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Watch on YouTube", url=source_url, style=STYLE_PRIMARY)]])
