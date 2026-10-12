#!/usr/bin/env python3
"""Generate the ``TELEGRAM_SESSION_STRING`` the bot uses for large uploads.

Run this **once, on your own machine** (it asks for a login code, so it needs a terminal):

    python scripts/make_session.py

You are asked for the api_id/api_hash from https://my.telegram.org (or read from the
environment), the phone number of the account that will do the uploading, the login code
Telegram sends, and your 2FA password if you have one. What it prints is a session string:
a long credential that *is* the logged-in account. Treat it exactly like a password.

Then set it on the server:

    TELEGRAM_API_ID=123456
    TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
    TELEGRAM_SESSION_STRING=...

Notes
-----
* Use a fresh account if you can. Uploads count towards that account's history.
* A Premium account lifts the per-file ceiling from 2000 MB to 4000 MB; this script tells
  you which applies.
* The session can be revoked at any time from Telegram → Settings → Devices.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _prompt_int(name: str, env: str) -> int:
    value = os.getenv(env, "").strip()
    while not value.isdigit():
        value = input(f"{name}: ").strip()
    return int(value)


def _prompt(name: str, env: str) -> str:
    value = os.getenv(env, "").strip()
    while not value:
        value = input(f"{name}: ").strip()
    return value


async def main() -> int:
    try:
        from pyrogram import Client
    except ImportError:
        print("Pyrofork is not installed. Run:  pip install pyrofork", file=sys.stderr)
        return 2

    print("═" * 60)
    print("  Telegram session for large uploads (MTProto)")
    print("═" * 60)

    api_id = _prompt_int("api_id (from my.telegram.org)", "TELEGRAM_API_ID")
    api_hash = _prompt("api_hash (from my.telegram.org)", "TELEGRAM_API_HASH")

    client = Client(
        name=":memory:",
        api_id=api_id,
        api_hash=api_hash,
        in_memory=True,
        device_model="YouTube Downloader Bot",
    )

    print("\nLogging in — Telegram will send a login code to your app shortly.\n")
    await client.start()

    me = await client.get_me()
    session_string = await client.export_session_string()
    premium = bool(getattr(me, "is_premium", False))
    ceiling = 4000 if premium else 2000

    print("\n" + "═" * 60)
    print(f"  Signed in as {' '.join(p for p in (me.first_name, me.last_name) if p)} (@{me.username or '-'})")
    print(f"  Account id : {me.id}")
    print(f"  Premium    : {'yes' if premium else 'no'}")
    print(f"  Upload cap : {ceiling} MB per file")
    print("═" * 60)
    print("\nTELEGRAM_SESSION_STRING:\n")
    print(session_string)
    print(
        "\nCopy the value above into your server's environment. "
        f"Keep it secret — it is a full login for account {me.id}."
    )
    if not premium:
        print(
            "\nTip: the account is not on Telegram Premium, so uploads stop at 2000 MB. "
            "Premium would raise it to 4000 MB."
        )
    await client.stop()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130) from None
