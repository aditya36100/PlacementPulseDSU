"""Cloud worker: listens to Telegram 24/7 and writes into Supabase."""
import asyncio

from dotenv import load_dotenv

load_dotenv()

from app.telegram_client import start_listener  # noqa: E402


async def main():
    while True:
        try:
            await start_listener()
        except Exception as e:  # keep the worker alive, retry shortly
            print(f"[worker] listener stopped: {e!r} - restarting in 30s")
        await asyncio.sleep(30)


if __name__ == "__main__":
    asyncio.run(main())
