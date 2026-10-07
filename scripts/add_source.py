"""Add a source channel to the local database for development."""

import argparse
import asyncio

from sqlalchemy import select

from postradar.config import Settings
from postradar.db.models import Source
from postradar.db.session import create_session_factory, init_db


async def add_source(chat_id: int, username: str | None, title: str | None) -> None:
    settings = Settings()
    await init_db(settings.database_url)
    factory = create_session_factory(settings.database_url)
    try:
        async with factory() as session:
            existing = await session.scalar(
                select(Source).where(Source.telegram_chat_id == chat_id)
            )
            if existing is None:
                session.add(
                    Source(
                        telegram_chat_id=chat_id,
                        username=username,
                        title=title,
                        enabled=True,
                    )
                )
                await session.commit()
                print(f"Added enabled source {chat_id}")
            else:
                print(f"Source {chat_id} already exists (enabled={existing.enabled})")
    finally:
        await factory.kw["bind"].dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chat_id", type=int, help="Telegram channel ID, for example -100123...")
    parser.add_argument("--username")
    parser.add_argument("--title")
    args = parser.parse_args()
    asyncio.run(add_source(args.chat_id, args.username, args.title))


if __name__ == "__main__":
    main()
