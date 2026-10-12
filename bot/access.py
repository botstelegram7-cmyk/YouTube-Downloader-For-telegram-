"""Who may use the bot, and who may see the diagnostics.

The bot is deliberately **open**: anybody who finds it can download. What is restricted is the
*operator* surface — ``/diag`` and the detailed ``/health`` payload — which is limited to the
owner.

The owner can be given as ``OWNER_USERNAME`` (a ``@username``, resolved to an id at startup via
Telegram's public API) or as ``OWNER_USER_ID``. If neither is set, the bot is open *and* there
is no diagnostics operator: ``/diag`` says so instead of silently doing nothing, and the
detailed health payload stays closed.
"""

from __future__ import annotations

import logging
import time

from telegram import Update
from telegram.error import TelegramError

log = logging.getLogger("bot.access")


class Access:
    """Owner resolution and the per-user start cooldown."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self._owner_id = settings.owner_user_id or 0
        self._cooldowns: dict[int, float] = {}

    # -- owner ----------------------------------------------------------------
    @property
    def owner_id(self) -> int:
        """The owner's numeric id: the configured one, or the one resolved from a username."""
        return self._owner_id or self.settings.owner_user_id or 0

    @property
    def owner_label(self) -> str:
        if self.settings.owner_username:
            return f"@{self.settings.owner_username}"
        return str(self.owner_id) if self.owner_id else "not configured"

    @property
    def configured(self) -> bool:
        return bool(self.owner_id or self.settings.owner_username)

    def is_owner(self, user_id: int | None) -> bool:
        """True only for the configured owner — never a default-allow."""
        return bool(user_id) and self.owner_id != 0 and int(user_id) == int(self.owner_id)

    async def resolve_owner(self, bot) -> int:
        """Turn ``OWNER_USERNAME`` into a user id, once, at startup.

        ``get_chat("@name")`` is a public lookup, which is why a username is enough and why
        nobody has to hunt for a numeric id. It is tolerant on purpose: a typo must not stop
        the bot from starting, it just leaves ``/diag`` pointing at the numeric id instead.
        """
        username = self.settings.owner_username
        if not username or self._owner_id:
            return self._owner_id

        try:
            chat = await bot.get_chat(f"@{username}")
        except TelegramError as exc:
            log.warning(
                "Could not resolve OWNER_USERNAME=@%s (%s). Set OWNER_USER_ID instead — "
                "get your id by sending /id to the bot.",
                username,
                exc,
            )
            return 0

        if getattr(chat, "type", "") != "private":
            log.warning("@%s is not a user account; the diagnostics owner is unset", username)
            return 0

        self._owner_id = int(chat.id)
        log.info("Owner resolved: @%s (id %s) — /diag is owner-only", username, self._owner_id)
        return self._owner_id

    # -- cooldown -------------------------------------------------------------
    def cooldown_remaining(self, user_id: int) -> float:
        """Seconds this user must still wait before starting another download.

        The owner is exempt: an operator testing the bot should never be told to wait.
        """
        seconds = self.settings.user_cooldown_seconds
        if seconds <= 0 or not user_id or self.is_owner(user_id):
            return 0.0
        last = self._cooldowns.get(user_id)
        if last is None:
            return 0.0
        return max(0.0, seconds - (time.monotonic() - last))

    def mark_request(self, user_id: int) -> None:
        """Remember that this user just started something (there is no penalty for spam)."""
        if user_id:
            self._cooldowns[user_id] = time.monotonic()

    def forget(self, user_id: int) -> None:
        self._cooldowns.pop(user_id, None)


def is_owner(update: Update, access: Access) -> bool:
    """Convenience for handlers: is the caller of this update the owner?"""
    user = update.effective_user
    return access.is_owner(user.id if user else None)
