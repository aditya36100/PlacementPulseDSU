-- Run once in Supabase: SQL Editor -> New query -> Run
create table if not exists placements (
  id bigint generated always as identity primary key,
  chat_id text not null,
  message_id bigint not null,
  group_name text not null default '',
  date timestamptz,
  company text, roles text, eligibility jsonb, ctc text, deadline text, mode text,
  summary text, raw_message text,
  eligibility_branches jsonb, eligibility_batch text,
  eligibility_min_cgpa real, eligibility_min_10 real, eligibility_min_12 real,
  eligibility_backlogs text, package_lpa real, deadline_date text,
  location text, position_type text,
  has_ppo boolean default false, tech_stack text, tech_stack_inferred boolean default false,
  prep_topics text, prep_plan jsonb, application_link text default '',
  created_at timestamptz default now(),
  unique (chat_id, message_id)
);
create index if not exists placements_date_idx on placements (date desc);

create table if not exists processed_messages (
  chat_id text not null, message_id bigint not null,
  processed_at timestamptz default now(), primary key (chat_id, message_id)
);
create table if not exists backfill_state (
  chat_id text primary key, last_message_id bigint not null default 0,
  last_message_date text, updated_at timestamptz default now()
);
create table if not exists company_domains (company text primary key, domain text);

-- Only signed-in users (you) can read; the worker writes with the service key (bypasses RLS).
alter table placements enable row level security;
alter table processed_messages enable row level security;
alter table backfill_state enable row level security;
alter table company_domains enable row level security;
create policy "read placements" on placements for select to authenticated using (true);
create policy "read domains" on company_domains for select to authenticated using (true);

create or replace view placements_feed with (security_invoker = true) as
  select p.*, cd.domain as company_domain
  from placements p left join company_domains cd on cd.company = p.company;

-- Push new rows to the app instantly
alter publication supabase_realtime add table placements;
