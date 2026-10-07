"""Perform the one-time interactive Telethon user authorization."""

import asyncio
import getpass

from telethon import TelegramClient

from postradar.config import Settings
from postradar.telegram.source_client import require_user_account, validate_telegram_settings


def prompt_phone() -> str:
    return input("Telegram phone number: ").strip()


def prompt_code() -> str:
    return input("Telegram login code: ").strip()


def prompt_password() -> str:
    return getpass.getpass("Telegram 2FA password: ")


async def main() -> None:
    settings = Settings()
    validate_telegram_settings(
        settings.telegram_api_id,
        settings.telegram_api_hash,
        settings.telegram_session,
    )
    client = TelegramClient(
        settings.telegram_session,
        settings.telegram_api_id,
        settings.telegram_api_hash,
    )
    try:
        await client.start(
            phone=prompt_phone,
            code_callback=prompt_code,
            password=prompt_password,
            bot_token=None,
        )
        await require_user_account(client)
        print("Telethon user session authorized successfully.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
