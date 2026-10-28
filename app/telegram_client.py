import os
import re
from datetime import datetime, timezone, timedelta

from telethon import TelegramClient, events, utils
from telethon.sessions import StringSession

from app.extractor import extract_placement_info, lookup_company_domains
from app.push import notify_new_placement
from app.db import (
    get_backfill_state,
    get_companies_missing_domain,
    insert_placement,
    mark_message_processed,
    message_processed,
    purge_old_data,
    set_company_domain,
    set_last_backfill_id,
)


API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

GROUPS = [
    g.strip()
    for g in os.environ["TELEGRAM_GROUP"].split(",")
    if g.strip()
]

GROUPS = [
    int(g) if g.lstrip("-").isdigit() else g
    for g in GROUPS
]

SESSION_NAME = os.environ.get(
    "TELEGRAM_SESSION_NAME",
    "placement_session",
)

BACKFILL_LIMIT = int(
    os.environ.get("BACKFILL_LIMIT", "200")
)

RETENTION_DAYS = int(
    os.environ.get("RETENTION_DAYS", "7")
)

_SESSION_STRING = os.environ.get("TELEGRAM_SESSION_STRING", "").strip()

client = TelegramClient(
    StringSession(_SESSION_STRING) if _SESSION_STRING else SESSION_NAME,
    API_ID,
    API_HASH,
)


_CHAT_KEYS = {}


async def _chat_key(group):
    """Canonical chat id (same form as event.chat_id), whether the group is configured by id or username."""
    if group not in _CHAT_KEYS:
        _CHAT_KEYS[group] = str(utils.get_peer_id(await client.get_input_entity(group)))
    return _CHAT_KEYS[group]


_GROUP_NAMES = {}
_NAME_OVERRIDES = [
    n.strip() for n in os.environ.get("TELEGRAM_GROUP_NAMES", "").split(",") if n.strip()
]


async def _init_groups():
    """Map each chat id to a display name (shown as a tab in the app)."""
    for i, group in enumerate(GROUPS):
        key = await _chat_key(group)
        if i < len(_NAME_OVERRIDES):
            name = _NAME_OVERRIDES[i]
        else:
            ent = await client.get_entity(group)
            name = getattr(ent, "title", None) or getattr(ent, "first_name", None) or str(group)
        _GROUP_NAMES[key] = name
        print(f"[groups] {key} -> {name}")


_URL_RX = re.compile(
    r"(?:https?://|www\.)[^\s<>\"']+|\b(?:forms\.gle|bit\.ly|tinyurl\.com|lnkd\.in)/[^\s<>\"']+",
    re.I,
)
_SKIP_HOSTS = ("t.me", "telegram.me", "telegram.dog", "chat.whatsapp.com", "wa.me")
_FORM_HINTS = ("forms.gle", "docs.google.com/forms", "forms.office.com", "forms.microsoft.com", "typeform.com", "airtable.com")
_APPLY_HINTS = ("regist", "apply", "career", "jobs", "hiring", "signup", "sign-up", "form", "walkin",
                "walk-in", "recruit", "internship", "unstop", "naukri", "lever.co", "greenhouse.io", "workday")


def _clean_url(u):
    u = u.strip().rstrip(".,;:!?)]}>\"'*")
    if not re.match(r"https?://", u, re.I):
        u = "https://" + u
    return u


def find_link(msg, text):
    """Best registration / application URL in a Telegram message.

    Looks at hidden hyperlinks (text like 'Register here'), visible URLs and
    inline buttons. Google Forms win; Telegram/WhatsApp group links are ignored.
    """
    urls = []
    try:
        if msg is not None:
            for ent, ent_text in (msg.get_entities_text() or []):
                u = getattr(ent, "url", None)
                if u:
                    urls.append(u)
                elif type(ent).__name__ == "MessageEntityUrl":
                    urls.append(ent_text)
            for row in (msg.buttons or []):
                for b in row:
                    u = getattr(b, "url", None)
                    if u:
                        urls.append(u)
    except Exception as e:
        print(f"[links] could not read entities: {e}")
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


async def _store_if_relevant(chat_id, message_id, date, text, msg=None, notify=False):
    # Don't send the same Telegram message to Gemini again
    if message_processed(chat_id, message_id):
        print(f"[=] already processed: {message_id}")
        return True  # already finished, safe to advance past

    info = await extract_placement_info(text, message_date=str(date))

    # Only mark the message as processed after Gemini returns a result.
    # If Gemini fails, we'll be able to retry it later.
    if info is None:
        print(f"[!] extraction failed, will retry later: {message_id}")
        return False  # NOT finished — must not advance last_id past this

    if info.get("is_relevant"):
        insert_placement(
            chat_id=chat_id,
            message_id=message_id,
            date=str(date),
            company=info.get("company"),
            roles=info.get("roles"),
            eligibility=info.get("eligibility"),
            ctc=info.get("ctc"),
            deadline=info.get("deadline"),
            mode=info.get("mode"),
            summary=info.get("summary"),
            raw_message=text,
            eligibility_branches=info.get("eligibility_branches"),
            eligibility_batch=info.get("eligibility_batch"),
            eligibility_min_cgpa=info.get("eligibility_min_cgpa"),
            eligibility_min_10=info.get("eligibility_min_10"),
            eligibility_min_12=info.get("eligibility_min_12"),
            eligibility_backlogs=info.get("eligibility_backlogs"),
            package_lpa=info.get("package_lpa"),
            deadline_date=info.get("deadline_date"),
            location=info.get("location"),
            position_type=info.get("type"),
            has_ppo=info.get("has_ppo"),
            tech_stack=info.get("tech_stack"),
            tech_stack_inferred=info.get("tech_stack_inferred"),
            prep_topics=info.get("prep_topics"),
            application_link=find_link(msg, text),
            group_name=_GROUP_NAMES.get(str(chat_id), ""),
        )

        print(
            f"[+] stored: {info.get('company')} ({info.get('type')}) - "
            f"{info.get('summary')}"
        )
        if notify:  # live messages only; never spam during backfill
            await notify_new_placement(
                info.get("company"), _GROUP_NAMES.get(str(chat_id), ""),
                info.get("summary"), chat_id, message_id,
            )
    else:
        print("[-] skipped irrelevant message")

    # Remember that this message has already been analyzed
    mark_message_processed(chat_id, message_id)
    return True


async def handle_new_message(event):
    text = event.raw_text

    if not text or len(text.strip()) < 10:
        return

    await _store_if_relevant(
        str(event.chat_id),
        event.id,
        event.date,
        text,
        msg=event.message,
        notify=True,
    )

    try:
        await resolve_domains()
    except Exception as e:
        print(f"[domains] skipped: {e}")


async def backfill():
    """Fetch messages from the rolling 1-week window, resume from last saved ID.

    - First run  : fetches all messages up to RETENTION_DAYS days old.
    - Subsequent : resumes from the last processed message ID (only new msgs).
    - Always     : skips messages older than the rolling cutoff.
    - After done : purges DB rows that have fallen outside the retention window.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)

    for group in GROUPS:
        chat_key = await _chat_key(group)
        state = get_backfill_state(group)
        last_id = int(state["last_message_id"] or 0)

        print(
            f"[backfill] group={group} | last_id={last_id} | "
            f"cutoff={cutoff.date()} (rolling {RETENTION_DAYS}d)"
        )

        last_seen_id = last_id
        last_seen_message = None
        stopped_early = False

        while True:
            if last_id > 0 or last_seen_id > 0:
                messages = client.iter_messages(
                    group, limit=BACKFILL_LIMIT, min_id=last_seen_id, reverse=True
                )
            else:
                messages = client.iter_messages(
                    group, limit=None, offset_date=cutoff, reverse=True
                )
            batch = 0
            async for message in messages:
                batch += 1
                if message.date is not None:
                    msg_date = message.date
                    if msg_date.tzinfo is None:
                        msg_date = msg_date.replace(tzinfo=timezone.utc)
                    if msg_date < cutoff:
                        if last_id == 0:
                            break
                        if message.id > last_seen_id:
                            last_seen_id = message.id
                            last_seen_message = message
                        continue

                ok = True
                if message.raw_text:
                    ok = await _store_if_relevant(
                        chat_key,
                        message.id,
                        message.date,
                        message.raw_text,
                        msg=message,
                    )

                if not ok:
                    stopped_early = True
                    break

                if message.id > last_seen_id:
                    last_seen_id = message.id
                    last_seen_message = message

            # Resume mode returns at most BACKFILL_LIMIT per pass: keep going until drained.
            if stopped_early or last_id == 0 or batch < BACKFILL_LIMIT:
                break

        if last_seen_id > last_id:
            set_last_backfill_id(
                group,
                last_seen_id,
                last_message_date=(
                    str(last_seen_message.date)
                    if last_seen_message is not None
                    else None
                ),
            )
            print(f"[backfill] {group}: saved last_id={last_seen_id}")
        else:
            print(f"[backfill] {group}: no new messages found")

        if stopped_early:
            print(
                f"[backfill] {group}: stopped early after a failed "
                f"extraction — remaining messages will be retried next run"
            )

    purge_old_data(retention_days=RETENTION_DAYS)
    print("Backfill complete.")



async def resolve_domains():
    """Ask Gemini once for the website of every stored company that has none yet.

    The dashboard uses it to look up the right logo. Costs one request per 40 companies.
    """
    names = get_companies_missing_domain()
    for i in range(0, len(names), 40):
        found = await lookup_company_domains(names[i:i + 40])
        if found is None:
            break
        for name, domain in found.items():
            set_company_domain(name, domain)
        print(f"[domains] looked up websites for {len(found)} companies")


_handler_added = False


async def start_listener(run_backfill: bool = True):
    global _handler_added
    await client.start()  # type: ignore
    print("Telegram client connected.")

    await _init_groups()

    if not _handler_added:
        client.add_event_handler(handle_new_message, events.NewMessage(chats=GROUPS))
        _handler_added = True
    print(f"Listening for new messages in {list(_GROUP_NAMES.values())} ...")

    if run_backfill:
        await backfill()

    try:
        await resolve_domains()
    except Exception as e:
        print(f"[domains] skipped: {e}")

    await client.run_until_disconnected()
