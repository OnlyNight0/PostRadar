"""Set a source's destination channel for local development."""

import argparse
import asyncio

from postradar.config import Settings
from postradar.db.models import Source
from postradar.db.session import create_session_factory, init_db


async def set_destination(source_id: int, destination_channel_id: int) -> bool:
    settings = Settings()
    await init_db(settings.database_url)
    factory = create_session_factory(settings.database_url)
    try:
        async with factory() as session:
            source = await session.get(Source, source_id)
            if source is None:
                print(f"Source {source_id} was not found")
                return False
            source.destination_channel_id = destination_channel_id
            await session.commit()
            print(f"Set destination {destination_channel_id} for source {source_id}")
            return True
    finally:
        await factory.kw["bind"].dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_id", type=int, help="Local Source.id")
    parser.add_argument("destination_channel_id", type=int, help="Telegram destination channel ID")
    args = parser.parse_args()
    if not asyncio.run(set_destination(args.source_id, args.destination_channel_id)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
