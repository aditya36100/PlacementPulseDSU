"""Multi-user poller: each enabled chat is read once per run through one user's Telegram session."""
import asyncio
import os
import re
from datetime import datetime, timezone, timedelta

from cryptography.fernet import Fernet
from telethon import TelegramClient
from telethon.sessions import StringSession

from app.db import sb, message_processed, mark_message_processed, insert_placement, \
    get_backfill_state, set_last_backfill_id, purge_old_data

RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))
BACKFILL_LIMIT = int(os.environ.get("BACKFILL_LIMIT", "200"))
MAX_PER_USER = int(os.environ.get("MAX_EXTRACT_PER_USER", "60"))
STAGGER_SECONDS = int(os.environ.get("STAGGER_SECONDS", "8"))
DEFAULT_API_ID = int(os.environ["TELEGRAM_API_ID"])
DEFAULT_API_HASH = os.environ["TELEGRAM_API_HASH"]
SERVER_KEY = os.environ.get("GEMINI_API_KEY")
fernet = Fernet(os.environ["ENC_KEY"].encode())

_URL_RX = re.compile(r"(?:https?://|www\.)[^\s<>\"']+|\b(?:forms\.gle|bit\.ly|tinyurl\.com|lnkd\.in)/[^\s<>\"']+", re.I)
_SKIP_HOSTS = ("t.me", "telegram.me", "telegram.dog", "chat.whatsapp.com", "wa.me")
_FORM_HINTS = ("forms.gle", "docs.google.com/forms", "forms.office.com", "forms.microsoft.com", "typeform.com", "airtable.com")
_APPLY_HINTS = ("regist", "apply", "career", "jobs", "hiring", "signup", "sign-up", "form", "walkin",
                "walk-in", "recruit", "internship", "unstop", "naukri", "lever.co", "greenhouse.io", "workday")


def dec(t):
    return fernet.decrypt(t.encode()).decode()


def _clean_url(u):
    u = u.strip().rstrip(".,;:!?)]}>\"'*")
    return u if re.match(r"https?://", u, re.I) else "https://" + u


def find_link(msg, text):
    urls = []
    try:
        for ent, ent_text in (msg.get_entities_text() or []):
            u = getattr(ent, "url", None)
            if u:
                urls.append(u)
            elif type(ent).__name__ == "MessageEntityUrl":
                urls.append(ent_text)
        for row in (msg.buttons or []):
            for b in row:
                if getattr(b, "url", None):
                    urls.append(b.url)
    except Exception:
        pass
    urls += _URL_RX.findall(text or "")
    best, best_score, seen = None, -1, set()
    for raw in urls:
        u = _clean_url(raw)
        low = u.lower()
        if low in seen:
            continue
        seen.add(low)
        host = re.sub(r"^https?://(www\.)?", "", low).split("/")[0]
        if host in _SKIP_HOSTS:
            continue
        score = 100 if any(h in low for h in _FORM_HINTS) else 50 if any(h in low for h in _APPLY_HINTS) else 10
        if score > best_score:
            best, best_score = u, score
    return best


def load_plan():
    """Return {user_id: {"chats": [(chat_id, title)], "secrets": row, "has_key": bool}}.
    Each enabled chat is assigned to exactly one user (prefer users with their own Gemini key)."""
    chats = sb().table("user_chats").select("user_id,chat_id,title").eq("enabled", True).execute().data or []
    secrets = {r["user_id"]: r for r in
               (sb().table("user_secrets").select("*").not_.is_("telegram_session_enc", "null").execute().data or [])}
    by_chat = {}
    for c in chats:
        if c["user_id"] in secrets:
            by_chat.setdefault(c["chat_id"], []).append(c)
    plan = {}
    for chat_id, rows in by_chat.items():
        rows.sort(key=lambda r: (not secrets[r["user_id"]].get("gemini_key_enc"), r["user_id"]))
        owner = rows[0]
        p = plan.setdefault(owner["user_id"], {"chats": [], "secrets": secrets[owner["user_id"]]})
        p["chats"].append((chat_id, owner["title"] or ""))
    return plan


# ---------- part 2: reading, extracting, notifying ----------
import httpx
from app.extractor import extract_placement_info

EXPO_URL = "https://exp.host/--/api/v2/push/send"


def notify_chat(chat_id, title, company, summary, message_id):
    users = [r["user_id"] for r in sb().table("user_chats").select("user_id")
             .eq("chat_id", chat_id).eq("enabled", True).execute().data or []]
    if not users:
        return
    tokens = [r["token"] for r in sb().table("push_tokens").select("token").in_("user_id", users).execute().data or []]
    head = (company or "New update") + (f" \u00b7 {title}" if title else "")
    dead = []
    for i in range(0, len(tokens), 100):
        chunk = tokens[i:i + 100]
        msgs = [{"to": t, "title": head, "body": summary or "", "sound": "default", "channelId": "placements",
                 "priority": "high", "data": {"chat_id": chat_id, "message_id": message_id}} for t in chunk]
        try:
            res = httpx.post(EXPO_URL, json=msgs, timeout=15).json().get("data", [])
        except Exception as e:
            print(f"[push] failed: {e!r}")
            return
        for t, r in zip(chunk, res):
            if r.get("status") == "error" and (r.get("details") or {}).get("error") == "DeviceNotRegistered":
                dead.append(t)
    if dead:
        sb().table("push_tokens").delete().in_("token", dead).execute()


async def store_one(chat_id, title, message, gem_key, budget, notify):
    """Returns (done, used_extraction). done=False means: stop and retry this message next run."""
    text = message.raw_text
    if not text or len(text.strip()) < 10:
        return True, False
    if message_processed(chat_id, message.id):
        return True, False
    if budget[0] <= 0:
        return False, False
    info = await extract_placement_info(text, message_date=str(message.date), api_key=gem_key)
    if info is None:
        return False, False
    used = bool(info.get("is_relevant")) or True
    budget[0] -= 1
    if info.get("is_relevant"):
        insert_placement(
            chat_id=chat_id, message_id=message.id, date=str(message.date),
            company=info.get("company"), roles=info.get("roles"), eligibility=info.get("eligibility"),
            ctc=info.get("ctc"), deadline=info.get("deadline"), mode=info.get("mode"),
            summary=info.get("summary"), raw_message=text,
            eligibility_branches=info.get("eligibility_branches"), eligibility_batch=info.get("eligibility_batch"),
            eligibility_min_cgpa=info.get("eligibility_min_cgpa"), eligibility_min_10=info.get("eligibility_min_10"),
            eligibility_min_12=info.get("eligibility_min_12"), eligibility_backlogs=info.get("eligibility_backlogs"),
            package_lpa=info.get("package_lpa"), deadline_date=info.get("deadline_date"),
            location=info.get("location"), position_type=info.get("type"), has_ppo=info.get("has_ppo"),
            tech_stack=info.get("tech_stack"), tech_stack_inferred=info.get("tech_stack_inferred"),
            prep_topics=info.get("prep_topics"), application_link=find_link(message, text), group_name=title,
        )
        print("[+] stored a placement")
        if notify:
            notify_chat(chat_id, title, info.get("company"), info.get("summary"), message.id)
    mark_message_processed(chat_id, message.id)
    return True, used


async def read_chat(client, chat_id, title, gem_key, budget):
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    last_id = int(get_backfill_state(chat_id)["last_message_id"] or 0)
    last_seen, last_msg, stopped = last_id, None, False
    entity = await client.get_input_entity(int(chat_id))
    while True:
        if last_seen > 0:
            it = client.iter_messages(entity, limit=BACKFILL_LIMIT, min_id=last_seen, reverse=True)
        else:
            it = client.iter_messages(entity, limit=None, offset_date=cutoff, reverse=True)
        batch = 0
        async for m in it:
            batch += 1
            d = m.date if m.date is None or m.date.tzinfo else m.date.replace(tzinfo=timezone.utc)
            if d is not None and d < cutoff:
                if last_id == 0:
                    break
                if m.id > last_seen:
                    last_seen, last_msg = m.id, m
                continue
            ok, _ = await store_one(chat_id, title, m, gem_key, budget, notify=last_id > 0)
            if not ok:
                stopped = True
                break
            if m.id > last_seen:
                last_seen, last_msg = m.id, m
        if stopped or last_id == 0 or batch < BACKFILL_LIMIT:
            break
    if last_seen > last_id:
        set_last_backfill_id(chat_id, last_seen, last_message_date=str(last_msg.date) if last_msg else None)
    print(f"[read] {title or chat_id}: last_id={last_seen}{' (stopped early)' if stopped else ''}")


def mark_disconnected(user_id):
    sb().table("user_secrets").update({"telegram_session_enc": None}).eq("user_id", user_id).execute()
    sb().table("user_settings").update({"telegram_connected": False}).eq("user_id", user_id).execute()


async def run_user(user_id, p, is_owner):
    s = p["secrets"]
    api_id = int(s["tg_api_id"]) if s.get("tg_api_id") and s.get("tg_api_hash_enc") else DEFAULT_API_ID
    api_hash = dec(s["tg_api_hash_enc"]) if s.get("tg_api_id") and s.get("tg_api_hash_enc") else DEFAULT_API_HASH
    gem_key = dec(s["gemini_key_enc"]) if s.get("gemini_key_enc") else None
    if not gem_key:
        if not is_owner or not SERVER_KEY:
            print(f"[user {user_id[:8]}] no Gemini key, skipping {len(p['chats'])} chat(s)")
            return
        gem_key = None  # None = the server key (owner only)
    budget = [MAX_PER_USER]
    client = TelegramClient(StringSession(dec(s["telegram_session_enc"])), api_id, api_hash,
                            device_model="PlacementPulse", system_version="Cloud", app_version="1.0")
    await client.connect()
    try:
        if not await client.is_user_authorized():
            print(f"[user {user_id[:8]}] session no longer valid")
            mark_disconnected(user_id)
            return
        for chat_id, title in p["chats"]:
            try:
                await read_chat(client, chat_id, title, gem_key, budget)
            except Exception as e:
                print(f"[read] {title or chat_id} failed: {e!r}")
    finally:
        await client.disconnect()


async def main():
    plan = load_plan()
    owner_id = os.environ.get("OWNER_USER_ID", "")
    print(f"[plan] {len(plan)} user(s), {sum(len(p['chats']) for p in plan.values())} chat(s)")
    for i, (uid, p) in enumerate(plan.items()):
        if i:
            await asyncio.sleep(STAGGER_SECONDS)
        try:
            await run_user(uid, p, uid == owner_id)
        except Exception as e:
            print(f"[user {uid[:8]}] failed: {e!r}")
    purge_old_data(retention_days=RETENTION_DAYS)
    print("Multi poll complete.")


if __name__ == "__main__":
    asyncio.run(main())
