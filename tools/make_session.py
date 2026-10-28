"""Run ONCE locally. Prints TELEGRAM_SESSION_STRING for the cloud worker.

Reuses your existing placement_session.session if present (no new login),
otherwise logs in interactively. Stop the old local app before using the string.
"""
import os
from dotenv import load_dotenv
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

load_dotenv()
api_id, api_hash = int(os.environ["TELEGRAM_API_ID"]), os.environ["TELEGRAM_API_HASH"]
name = os.environ.get("TELEGRAM_SESSION_NAME", "placement_session")

if os.path.exists(name + ".session"):
    c = TelegramClient(name, api_id, api_hash)
    c.connect()
    assert c.is_user_authorized(), "existing session is not logged in"
    print("\nTELEGRAM_SESSION_STRING=" + StringSession.save(c.session))
    c.disconnect()
else:
    with TelegramClient(StringSession(), api_id, api_hash) as c:
        print("\nTELEGRAM_SESSION_STRING=" + c.session.save())
