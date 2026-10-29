"""PlacementPulse Telegram login API (runs on Vercel).

Flow: send_code -> verify (code, then 2FA password if needed) -> sync / disconnect.
Sessions are encrypted with ENC_KEY before being stored in Supabase (user_secrets).
"""
import json
import os
import time
from datetime import datetime, timezone

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
DEFAULT_API_ID = os.environ.get("TG_API_ID", "")
DEFAULT_API_HASH = os.environ.get("TG_API_HASH", "")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "https://placementpulse-dsu.netlify.app")
fernet = Fernet(os.environ["ENC_KEY"].encode())

TICKET_TTL = 600  # seconds the login ticket stays valid
MAX_DIALOGS = 400

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOWED_ORIGIN],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
router = APIRouter()


# ---------- helpers ----------
def enc(text: str) -> str:
    return fernet.encrypt(text.encode()).decode()


def dec(token: str) -> str:
    try:
        return fernet.decrypt(token.encode()).decode()
    except InvalidToken:
        raise HTTPException(400, "Invalid or expired ticket")


def _svc_headers(extra=None):
    h = {"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
         "Content-Type": "application/json"}
    h.update(extra or {})
    return h


async def current_user_id(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Not signed in")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{SUPABASE_URL}/auth/v1/user",
                        headers={"apikey": SUPABASE_ANON_KEY, "Authorization": authorization})
    if r.status_code != 200:
        raise HTTPException(401, "Not signed in")
    return r.json()["id"]


async def db_upsert(table, rows, on_conflict, ignore_duplicates=False):
    prefer = "resolution=ignore-duplicates,return=minimal" if ignore_duplicates \
        else "resolution=merge-duplicates,return=minimal"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{SUPABASE_URL}/rest/v1/{table}?on_conflict={on_conflict}",
                         headers=_svc_headers({"Prefer": prefer}), content=json.dumps(rows))
    if r.status_code >= 300:
        raise HTTPException(500, f"Database error ({table})")


async def db_get_secrets(user_id):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{SUPABASE_URL}/rest/v1/user_secrets?user_id=eq.{user_id}&select=*",
                        headers=_svc_headers())
    rows = r.json() if r.status_code == 200 else []
    return rows[0] if rows else None


def make_client(session: str, api_id: int, api_hash: str) -> TelegramClient:
    return TelegramClient(StringSession(session), api_id, api_hash,
                          device_model="PlacementPulse", system_version="Cloud", app_version="1.0")


def pick_api(api_id, api_hash):
    if api_id and api_hash:
        try:
            return int(api_id), str(api_hash).strip()
        except ValueError:
            raise HTTPException(400, "api_id must be a number")
    if not DEFAULT_API_ID or not DEFAULT_API_HASH:
        raise HTTPException(500, "Server has no default Telegram API keys")
    return int(DEFAULT_API_ID), DEFAULT_API_HASH


async def list_chats(client):
    out = []
    async for d in client.iter_dialogs(limit=MAX_DIALOGS):
        if d.is_group or d.is_channel:
            out.append({"chat_id": str(d.id), "title": (d.name or "")[:120]})
    return out


async def save_chats(user_id, chats):
    if chats:
        rows = [{"user_id": user_id, "chat_id": c["chat_id"], "title": c["title"], "enabled": False}
                for c in chats]
        await db_upsert("user_chats", rows, "user_id,chat_id", ignore_duplicates=True)


# ---------- models ----------
class SendCode(BaseModel):
    phone: str
    api_id: str | None = None
    api_hash: str | None = None


class Verify(BaseModel):
    ticket: str
    code: str | None = None
    password: str | None = None


# ---------- routes ----------
@router.post("/tg/send_code")
async def send_code(body: SendCode, authorization: str | None = Header(default=None)):
    user_id = await current_user_id(authorization)
    api_id, api_hash = pick_api(body.api_id, body.api_hash)
    phone = body.phone.strip().replace(" ", "")
    client = make_client("", api_id, api_hash)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        ticket = enc(json.dumps({
            "u": user_id, "t": int(time.time()), "stage": "code", "phone": phone,
            "hash": sent.phone_code_hash, "session": client.session.save(),
            "api_id": api_id, "api_hash": api_hash, "custom": bool(body.api_id and body.api_hash),
        }))
    except PhoneNumberInvalidError:
        raise HTTPException(400, "That phone number looks invalid. Use +91XXXXXXXXXX.")
    except FloodWaitError as e:
        raise HTTPException(429, f"Telegram says wait {e.seconds} seconds and try again.")
    finally:
        await client.disconnect()
    return {"ticket": ticket}


@router.post("/tg/verify")
async def verify(body: Verify, authorization: str | None = Header(default=None)):
    user_id = await current_user_id(authorization)
    t = json.loads(dec(body.ticket))
    if t["u"] != user_id or time.time() - t["t"] > TICKET_TTL:
        raise HTTPException(400, "Login expired. Start again.")
    client = make_client(t["session"], t["api_id"], t["api_hash"])
    await client.connect()
    try:
        try:
            if t["stage"] == "code":
                if not body.code:
                    raise HTTPException(400, "Enter the code from Telegram")
                await client.sign_in(phone=t["phone"], code=body.code.strip(),
                                     phone_code_hash=t["hash"])
            else:
                if not body.password:
                    raise HTTPException(400, "Enter your Telegram 2-step password")
                await client.sign_in(password=body.password)
        except SessionPasswordNeededError:
            if not body.password:
                t.update({"stage": "password", "session": client.session.save(), "t": int(time.time())})
                return {"need_password": True, "ticket": enc(json.dumps(t))}
            await client.sign_in(password=body.password)
        except PhoneCodeInvalidError:
            raise HTTPException(400, "Wrong code. Check it and try again.")
        except PhoneCodeExpiredError:
            raise HTTPException(400, "Code expired. Go back and request a new one.")
        except PasswordHashInvalidError:
            raise HTTPException(400, "Wrong 2-step password.")

        session = client.session.save()
        chats = await list_chats(client)
    finally:
        await client.disconnect()

    await db_upsert("user_secrets", [{
        "user_id": user_id, "telegram_session_enc": enc(session),
        "tg_api_id": t["api_id"] if t["custom"] else None,
        "tg_api_hash_enc": enc(t["api_hash"]) if t["custom"] else None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }], "user_id")
    await db_upsert("user_settings", [{
        "user_id": user_id, "telegram_connected": True, "phone_hint": "..." + t["phone"][-4:],
    }], "user_id")
    await save_chats(user_id, chats)
    return {"connected": True, "chats": len(chats)}


async def _client_for(user_id):
    s = await db_get_secrets(user_id)
    if not s or not s.get("telegram_session_enc"):
        raise HTTPException(400, "Telegram is not connected")
    api_id, api_hash = pick_api(s.get("tg_api_id"),
                                dec(s["tg_api_hash_enc"]) if s.get("tg_api_hash_enc") else None)
    return make_client(dec(s["telegram_session_enc"]), api_id, api_hash)


@router.post("/tg/sync")
async def sync(authorization: str | None = Header(default=None)):
    user_id = await current_user_id(authorization)
    client = await _client_for(user_id)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise HTTPException(400, "Telegram session expired. Reconnect Telegram.")
        chats = await list_chats(client)
    finally:
        await client.disconnect()
    await save_chats(user_id, chats)
    return {"chats": len(chats)}


@router.post("/tg/disconnect")
async def disconnect(authorization: str | None = Header(default=None)):
    user_id = await current_user_id(authorization)
    try:
        client = await _client_for(user_id)
        await client.connect()
        try:
            await client.log_out()
        finally:
            await client.disconnect()
    except HTTPException:
        pass
    except Exception:
        pass  # already revoked on Telegram's side; still clear our copy
    async with httpx.AsyncClient(timeout=20) as c:
        await c.patch(f"{SUPABASE_URL}/rest/v1/user_secrets?user_id=eq.{user_id}",
                      headers=_svc_headers(),
                      content=json.dumps({"telegram_session_enc": None, "tg_api_id": None,
                                          "tg_api_hash_enc": None}))
        await c.delete(f"{SUPABASE_URL}/rest/v1/user_chats?user_id=eq.{user_id}",
                       headers=_svc_headers())
    await db_upsert("user_settings", [{"user_id": user_id, "telegram_connected": False,
                                       "phone_hint": None}], "user_id")
    return {"connected": False}


@router.get("/health")
async def health():
    return {"ok": True}


app.include_router(router)
app.include_router(router, prefix="/api")
