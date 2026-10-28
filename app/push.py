"""Send a phone notification (Expo Push -> FCM) for each NEW live placement."""
import asyncio

import httpx

from app.db import sb

EXPO_URL = "https://exp.host/--/api/v2/push/send"


def _send(title, body, data):
    tokens = [r["token"] for r in sb().table("push_tokens").select("token").execute().data]
    dead = []
    for i in range(0, len(tokens), 100):
        chunk = tokens[i:i + 100]
        msgs = [{"to": t, "title": title, "body": body, "sound": "default",
                 "channelId": "placements", "priority": "high", "data": data} for t in chunk]
        res = httpx.post(EXPO_URL, json=msgs, timeout=15).json().get("data", [])
        for t, r in zip(chunk, res):
            if r.get("status") == "error" and (r.get("details") or {}).get("error") == "DeviceNotRegistered":
                dead.append(t)
    if dead:
        sb().table("push_tokens").delete().in_("token", dead).execute()


async def notify_new_placement(company, group_name, summary, chat_id, message_id):
    title = (company or "New update") + (f" · {group_name}" if group_name else "")
    try:
        await asyncio.to_thread(_send, title, summary or "", {"chat_id": str(chat_id), "message_id": message_id})
    except Exception as e:
        print(f"[push] failed: {e!r}")
