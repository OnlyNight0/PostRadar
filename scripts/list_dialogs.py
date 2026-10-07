"""List channel and supergroup IDs visible to the authorized Telegram account."""

import asyncio

from telethon import TelegramClient, utils
from telethon.tl.types import Channel

from postradar.config import Settings
from postradar.telegram.source_client import require_user_account, validate_telegram_settings


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
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError(
                "Telethon session is not authorized. Run: "
                "PYTHONPATH=. python scripts/authorize_telegram.py"
            )
        await require_user_account(client)

        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            if not isinstance(entity, Channel):
                continue
            chat_id = utils.get_peer_id(entity)
            username = getattr(entity, "username", None)
            print(f"{chat_id}\t{dialog.title}\t@{username if username else ''}")
    finally:
        if client.is_connected():
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
