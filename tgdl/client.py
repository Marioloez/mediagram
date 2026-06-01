from contextlib import asynccontextmanager

from telethon import TelegramClient

from tgdl.config import Config


@asynccontextmanager
async def telegram_client(config: Config | None = None):
    cfg = config or Config.load()
    client = TelegramClient(cfg.session, cfg.api_id, cfg.api_hash)
    await client.start(phone=cfg.phone)
    try:
        yield client
    finally:
        await client.disconnect()
