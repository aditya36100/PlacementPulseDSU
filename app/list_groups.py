import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

client = TelegramClient("placement_session", API_ID, API_HASH)

async def main():
    async for dialog in client.iter_dialogs():
        print(dialog.id, "-", dialog.name)

with client:
    client.loop.run_until_complete(main())