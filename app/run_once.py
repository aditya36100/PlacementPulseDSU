"""One-shot run for GitHub Actions: fetch new messages, store them, notify, exit."""
import asyncio

from dotenv import load_dotenv

load_dotenv()

from app import telegram_client as tc  # noqa: E402


async def main():
    await tc.client.start()
    await tc._init_groups()
    await tc.backfill()
    try:
        await tc.resolve_domains()
    except Exception as e:
        print(f"[domains] skipped: {e}")
    await tc.client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
