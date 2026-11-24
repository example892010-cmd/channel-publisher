"""Run Channel Publisher Bot."""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from dotenv import load_dotenv

from bot.app import ChannelPublisherBot
from bot.config import Settings


def configure_logging() -> None:
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def main() -> None:
    load_dotenv()
    configure_logging()
    settings = Settings.from_env()
    app = ChannelPublisherBot(settings)
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    bot_task = asyncio.create_task(app.start(), name="telegram-bot")
    stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-signal")
    try:
        done, _ = await asyncio.wait(
            {bot_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if bot_task in done:
            exception = bot_task.exception()
            if exception:
                raise exception
        else:
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
    finally:
        if not bot_task.done():
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
        if not stop_task.done():
            stop_task.cancel()
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())
