import os
from datetime import datetime, timezone, timedelta

from supabase import create_client

_client = None


def sb():
    global _client
    if _client is None:
        _client = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    return _client


def message_processed(chat_id, message_id):
    for table in ("processed_messages", "placements"):
        r = (sb().table(table).select("message_id")
             .eq("chat_id", str(chat_id)).eq("message_id", message_id).limit(1).execute())
        if r.data:
            return True
    return False


def mark_message_processed(chat_id, message_id):
    sb().table("processed_messages").upsert(
        {"chat_id": str(chat_id), "message_id": message_id},
        on_conflict="chat_id,message_id", ignore_duplicates=True,
    ).execute()


def insert_placement(
    message_id, date, company, roles, eligibility, ctc, deadline, mode, summary, raw_message,
    eligibility_branches=None, eligibility_batch=None, eligibility_min_cgpa=None,
    eligibility_min_10=None, eligibility_min_12=None, eligibility_backlogs=None,
    package_lpa=None, deadline_date=None, location=None, position_type=None,
    has_ppo=False, tech_stack=None, tech_stack_inferred=False, prep_topics=None,
    application_link=None, chat_id="", group_name="",
):
    row = {
        "chat_id": str(chat_id), "message_id": message_id, "group_name": group_name or "",
        "date": date, "company": company, "roles": roles, "eligibility": eligibility,
        "ctc": ctc, "deadline": deadline, "mode": mode, "summary": summary,
        "raw_message": raw_message,
        "eligibility_branches": eligibility_branches or None,
        "eligibility_batch": eligibility_batch,
        "eligibility_min_cgpa": eligibility_min_cgpa,
        "eligibility_min_10": eligibility_min_10,
        "eligibility_min_12": eligibility_min_12,
        "eligibility_backlogs": eligibility_backlogs,
        "package_lpa": package_lpa, "deadline_date": deadline_date,
        "location": location, "position_type": position_type,
        "has_ppo": bool(has_ppo), "tech_stack": tech_stack,
        "tech_stack_inferred": bool(tech_stack_inferred), "prep_topics": prep_topics,
        "application_link": application_link or "",
    }
    sb().table("placements").upsert(
        row, on_conflict="chat_id,message_id", ignore_duplicates=True
    ).execute()


def purge_old_data(retention_days: int = 7):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    p = sb().table("placements").delete().lt("date", cutoff).execute()
    m = sb().table("processed_messages").delete().lt("processed_at", cutoff).execute()
    print(f"[purge] deleted {len(p.data or [])} placements, {len(m.data or [])} processed records")


def get_backfill_state(chat_id):
    r = sb().table("backfill_state").select("*").eq("chat_id", str(chat_id)).limit(1).execute()
    if r.data:
        return r.data[0]
    return {"chat_id": str(chat_id), "last_message_id": 0, "last_message_date": None}


def set_last_backfill_id(chat_id, last_message_id, last_message_date=None):
    row = {"chat_id": str(chat_id), "last_message_id": int(last_message_id),
           "updated_at": datetime.now(timezone.utc).isoformat()}
    if last_message_date:
        row["last_message_date"] = last_message_date
    sb().table("backfill_state").upsert(row, on_conflict="chat_id").execute()


def get_companies_missing_domain():
    cos = {r["company"].strip() for r in
           sb().table("placements").select("company").not_.is_("company", "null").execute().data
           if (r["company"] or "").strip()}
    have = {r["company"] for r in sb().table("company_domains").select("company").execute().data}
    return sorted(cos - have)


def set_company_domain(company, domain):
    sb().table("company_domains").upsert(
        {"company": company, "domain": domain or ""}, on_conflict="company"
    ).execute()
