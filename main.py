"""Run Channel Publisher Bot."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from dotenv import load_dotenv

from bot.app import ChannelPublisherBot
from bot.config import Settings


class HealthCheckHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler to satisfy Render's port detection requirement."""
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")
        
    def log_message(self, format, *args):
        # Suppress logging of health check pings to keep logs clean
        return


def run_web_server() -> None:
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()


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

    # Start the dummy web server in a background thread so Render detects an open port
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

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
