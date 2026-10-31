#!/usr/bin/env python3
"""
Melbete Bingo - Main Entry Point

Runs the Telegram bot and/or the FastAPI server. Persistence is Cloud
Firestore (see src/core/firebase.py) - there is no local database to
initialize before starting.

Usage:
    python main.py            # run bot + API together
    python main.py bot        # run only the Telegram bot (long-polling)
    python main.py api        # run only the FastAPI server
"""
import logging
import sys
from multiprocessing import Process

import uvicorn

from src.core.config import settings

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def run_bot():
    """Run the Telegram bot (long-polling)."""
    from src.bot.telegram_bot import bot

    bot.setup()
    bot.run()


def run_api():
    """Run the FastAPI server."""
    uvicorn.run(
        "src.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )


def main():
    try:
        logger.info("Starting Melbete Bingo (env=%s)...", settings.app_env)

        mode = sys.argv[1] if len(sys.argv) > 1 else None

        if mode == "bot":
            logger.info("Starting in Bot-only mode...")
            run_bot()
        elif mode == "api":
            logger.info("Starting in API-only mode...")
            run_api()
        elif mode is None:
            logger.info("Starting in full mode (Bot + API)...")
            api_process = Process(target=run_api)
            api_process.start()
            try:
                run_bot()
            finally:
                api_process.terminate()
                api_process.join()
        else:
            logger.error("Unknown mode: %s", mode)
            print("Usage: python main.py [bot|api]")
            sys.exit(1)

    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:  # noqa: BLE001
        logger.error("Fatal error: %s", e, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
