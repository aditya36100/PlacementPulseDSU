import os
import re
import json
import time
import asyncio

from google import genai
from google.genai import types


client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

MODEL = os.environ.get(
    "EXTRACTION_MODEL",
    "gemini-3.5-flash-lite"
)

# Used when a message states a bare "minimum X%" without saying whether that's a
# CGPA or a percentage — treated as roughly CGPA (X / 9.5).
PERCENT_TO_CGPA_FACTOR = 9.5

SYSTEM_PROMPT = f"""You are monitoring a college placements Telegram group on behalf of a student
who wants to prepare thoroughly for every opportunity and check their own eligibility.

Decide whether the message is an actual placement/hiring/internship announcement (a company opening
applications, sharing eligibility criteria, deadlines, drive schedules, shortlist/result updates,
etc.) as opposed to casual chat, memes, thank-you notes, questions from other students, or unrelated
discussion. Interview schedules, shortlist results, and reminders ARE relevant (type "Other") even
though they are not a fresh hiring announcement — but general policy notices, leave rules, and admin
housekeeping that are not about a specific hiring process are NOT relevant.

Respond with a JSON object using exactly this shape:

{{
  "is_relevant": boolean,
  "company": string or null,
  "roles": string or null,
  "eligibility": array of short strings or null,
  "eligibility_branches": array of strings or null,
  "eligibility_batch": string or null,
  "eligibility_min_cgpa": number or null,
  "eligibility_min_10": number or null,
  "eligibility_min_12": number or null,
  "eligibility_backlogs": "no_active_backlogs_required" or null,
  "ctc": string or null,
  "package_lpa": number or null,
  "deadline": string or null,
  "deadline_date": string ("YYYY-MM-DD") or null,
  "location": string or null,
  "mode": string or null,
  "type": "Internship" or "Full-Time" or "Other",
  "has_ppo": boolean,
  "tech_stack": string or null,
  "tech_stack_inferred": boolean,
  "prep_topics": string or null,
  "summary": string or null
}}

Rules:
- If the message is not a placement-related announcement, set is_relevant to false and every
  other field to null/false as appropriate.
- Never invent details for company, roles, ctc, deadline, or location. If a field isn't
  mentioned, use null for it.
- "eligibility": break the stated criteria into 2-5 short, separate bullet strings. Use null if
  no eligibility is stated.
- "eligibility_branches": list of allowed branches/departments exactly as named (e.g. ["ECE",
  "EEE"]). Use null if the message says any branch / doesn't restrict by branch, or doesn't
  mention branches at all.
- "eligibility_batch": the graduating year(s) mentioned (e.g. "2027" or "2026 & 2027"). Null if
  not stated.
- "eligibility_min_cgpa": the minimum CGPA out of 10 required. If the message states a bare
  percentage without saying it's specifically Class 10 or Class 12 (e.g. "65% throughout"),
  convert it to a CGPA-equivalent using percent / {PERCENT_TO_CGPA_FACTOR}. If the message states
  an actual CGPA figure, use it directly. Null if no such requirement is stated.
- "eligibility_min_10": minimum Class 10 percentage required specifically, as a plain number.
  Null if not stated separately for Class 10.
- "eligibility_min_12": minimum Class 12 percentage required specifically, as a plain number.
  Null if not stated separately for Class 12.
- "eligibility_backlogs": "no_active_backlogs_required" if active backlogs disqualify a
  candidate; null if backlogs aren't mentioned at all.
- "package_lpa": your best-effort numeric estimate of the annual value in Lakhs Per Annum (LPA).
  Convert a monthly stipend to annualized LPA-equivalent (monthly * 12 / 100000). If a range is
  given, use the higher figure. Null if no figure at all.
- "deadline_date": if a specific calendar date for the deadline can be determined (resolve
  relative dates like "tomorrow" or "ASAP" using today's date as reference, and bare day/month
  with no year using the nearest future occurrence), output it as YYYY-MM-DD. Use null only if no
  specific date can be pinned down at all (e.g. deadline is truly just "ASAP" with zero date
  context anywhere in the message).
- "location": the city/cities where the role, interview, or exam is based (e.g. "Bengaluru",
  "Pan India"). If more than two locations are listed, name the top two. Null if not mentioned.
- "type": "Internship" for internship-only postings, "Full-Time" for full-time offers or direct
  full-time hiring, "Other" for anything relevant but not a specific hiring role (assessments,
  hackathons, shortlists, interview schedules, general announcements, competitions).
- "has_ppo": true if the message mentions a Pre-Placement Offer, PPO, PPI, or a stated path from
  this internship to a full-time role; false otherwise.
- "tech_stack": if the message explicitly states required technologies, extract them exactly. If
  not stated, use your general knowledge of what a role with this title typically requires at a
  company of this kind, and give your best inferred answer instead of null. Only use null if the
  role is too vague to infer anything reasonable.
- "tech_stack_inferred": true if tech_stack was inferred by you rather than stated explicitly.
- "prep_topics": for any technical hiring role, list standard preparation areas, comma-separated,
  most important first. Use null for "Other" type or non-technical roles.
- "summary": a single plain-English sentence a busy student can read in two seconds.
"""


# ---------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------

_MIN_INTERVAL_SECONDS = 4.5
_last_call_at = 0.0


# ---------------------------------------------------------
# Local placement filter
# ---------------------------------------------------------
# These keywords are checked BEFORE calling Gemini.
# This prevents Gemini API calls for obviously unrelated
# Telegram messages.
# ---------------------------------------------------------

PLACEMENT_KEYWORDS = {
    "placement", "internship", "intern", "hiring", "recruitment", "recruit", "job",
    "opening", "drive", "campus", "company", "eligible", "eligibility", "ctc", "lpa",
    "salary", "stipend", "deadline", "shortlist", "shortlisted", "assessment",
    "interview", "registration", "apply", "application", "joining", "offer",
}


def looks_like_placement_message(text: str) -> bool:
    """
    Quickly check whether a Telegram message looks like
    a placement/internship-related message.

    This runs locally and does NOT consume Gemini quota.
    """

    text_lower = text.lower()

    return any(
        keyword in text_lower
        for keyword in PLACEMENT_KEYWORDS
    )


# ---------------------------------------------------------
# Gemini daily quota state
# ---------------------------------------------------------

_QUOTA_RETRY_SECONDS = 3600
_quota_blocked_until = 0.0


def _quota_blocked() -> bool:
    return time.monotonic() < _quota_blocked_until


def _block_quota():
    global _quota_blocked_until
    _quota_blocked_until = time.monotonic() + _QUOTA_RETRY_SECONDS


def _is_daily_quota_error(message: str) -> bool:
    return (
        "GenerateRequestsPerDayPerProject-FreeTier" in message
        or "generate_content_free_tier_requests" in message
        or "Quota exceeded for metric" in message
    )


def _get_retry_delay(message: str) -> int:
    match = re.search(
        r"retryDelay['\"]?\s*:\s*['\"]?(\d+)",
        message,
    )

    if match:
        return int(match.group(1)) + 2

    return 20


# ---------------------------------------------------------
# Gemini rate limiter
# ---------------------------------------------------------

_rate_lock = asyncio.Lock()


async def _respect_rate_limit():
    global _last_call_at
    async with _rate_lock:
        wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _last_call_at)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call_at = time.monotonic()


# ---------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------

async def extract_placement_info(text: str, message_date: str = "") -> dict | None:
    """
    Classify a single Telegram message.

    Flow:

    Telegram message
          ↓
    Local keyword filter
          ↓
    Gemini only if potentially relevant
          ↓
    Structured JSON
    """

    if not looks_like_placement_message(text):
        print("[local filter] skipped non-placement message")
        return {"is_relevant": False}

    if _quota_blocked():
        return None

    prompt = f"Message posted on: {message_date}\n\n{text}" if message_date else text

    for attempt in range(4):

        await _respect_rate_limit()

        try:

            response = await client.aio.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0,
                ),
            )

            return json.loads(response.text)

        except Exception as e:

            msg = str(e)

            if _is_daily_quota_error(msg):

                _block_quota()

                print(
                    "[extractor] Gemini daily quota exhausted. "
                    "Pausing extraction for 1h, then retrying."
                )

                return None

            if "RESOURCE_EXHAUSTED" in msg or "429" in msg:

                delay = _get_retry_delay(msg)

                print(
                    f"[extractor] rate limited, "
                    f"waiting {delay}s before retry "
                    f"(attempt {attempt + 1}/4)..."
                )

                await asyncio.sleep(delay)

                continue

            print(f"[extractor error] {e}")

            return None

    print(
        "[extractor error] gave up after repeated "
        "rate limiting"
    )

    return None


# ---------------------------------------------------------
# Company website lookup (used to find each company's logo)
# ---------------------------------------------------------

_DOMAIN_RX = re.compile(r"^(?:[a-z0-9-]+\.)+[a-z]{2,}$")


def _clean_domain(value) -> str:
    d = str(value or "").strip().lower()
    d = re.sub(r"^[a-z]+://", "", d)
    d = d.split("/")[0].split("?")[0].strip()
    if d.startswith("www."):
        d = d[4:]
    return d if _DOMAIN_RX.match(d) else ""


async def lookup_company_domains(names):
    """One Gemini call: the official website domain of each company name.

    Returns {name: domain}, where "" means "not sure", or None when the call
    failed so the caller can simply try again on the next run.
    """
    names = [n for n in names if n]
    if not names:
        return {}
    if _quota_blocked():
        return None

    prompt = (
        "For each company name below, give the domain of the company's official website, "
        "for example \"google.com\" or \"tcs.com\". Use the company's main website, not a careers "
        "portal, a regional page or a social media profile. If you are not confident which "
        "company it is, use null. Respond with only a JSON object whose keys are the company "
        "names exactly as given and whose values are the domain or null.\n\nCompanies: "
        + json.dumps(names, ensure_ascii=False)
    )

    await _respect_rate_limit()
    try:
        response = await client.aio.models.generate_content(
            model=MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0,
            ),
        )
        data = json.loads(response.text)
    except Exception as e:
        if _is_daily_quota_error(str(e)):
            _block_quota()
        print(f"[extractor] company website lookup failed: {e}")
        return None

    if not isinstance(data, dict):
        return None
    return {n: _clean_domain(data.get(n)) for n in names}
