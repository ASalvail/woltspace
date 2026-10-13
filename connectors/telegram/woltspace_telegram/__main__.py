"""`python -m woltspace_telegram`: the Telegram connector process.

Environment: `WOLTSPACE_API` (the lodge, loopback), `TELEGRAM_BOT_TOKEN`,
and the connector token through the file `WOLTSPACE_CONNECTOR_TOKEN_FILE`
names (read once, then unlinked) or `WOLTSPACE_CONNECTOR_TOKEN`. Neither
token is ever on argv or in a log line. The supervisor does not launch this
yet (that is the next slice's seam, with the hardened handoff); until then
the owner runs it by hand after `woltspace connector runtime telegram
connector` and `woltspace connector token <instance>`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

import httpx

from . import LEASE_WAIT, LodgeRefused, TelegramConnector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("woltspace_telegram")


def _connector_token() -> str:
    path = os.environ.get("WOLTSPACE_CONNECTOR_TOKEN_FILE")
    if path:
        with open(path, encoding="utf-8") as handle:
            token = json.load(handle)["token"]
        os.unlink(path)
        return token
    token = os.environ.get("WOLTSPACE_CONNECTOR_TOKEN", "")
    if not token:
        sys.exit("woltspace_telegram: no connector token (WOLTSPACE_CONNECTOR_TOKEN_FILE)")
    return token


async def _main() -> int:
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        sys.exit("woltspace_telegram: TELEGRAM_BOT_TOKEN is not set")
    api = os.environ.get("WOLTSPACE_API", "http://127.0.0.1:7777")
    async with httpx.AsyncClient(base_url=api, timeout=60.0) as lodge, httpx.AsyncClient(
        base_url="https://api.telegram.org", timeout=httpx.Timeout(30.0, read=120.0),
    ) as bot:
        connector = TelegramConnector(lodge, bot, bot_token=bot_token, connector_token=_connector_token())
        try:
            state = await connector.start()
        except LodgeRefused:
            logger.error("the lodge refused the connector token; exiting")
            return 1
        logger.info("started: cursor=%s paused=%s", state.get("cursor"), state.get("paused"))

        async def inbound():
            while True:
                try:
                    await connector.poll_once()
                except LodgeRefused:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep polling
                    logger.warning("poll failed: %s", str(exc).replace(bot_token, "<token>"))
                    await asyncio.sleep(2)

        async def outbound():
            while True:
                try:
                    report = await connector.deliver_once(wait=LEASE_WAIT)
                    if report.paused:
                        await asyncio.sleep(5)
                except LodgeRefused:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep delivering
                    logger.warning("deliver failed: %s", str(exc).replace(bot_token, "<token>"))
                    await asyncio.sleep(2)

        try:
            await asyncio.gather(inbound(), outbound())
        except LodgeRefused:
            logger.error("the lodge refused the connector token; exiting")
            return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(_main()))
    except KeyboardInterrupt:
        sys.exit(0)
