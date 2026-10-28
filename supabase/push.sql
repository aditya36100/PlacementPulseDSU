-- Run once (after schema.sql): stores each phone's push token
create table if not exists push_tokens (
  token text primary key,
  user_id uuid not null default auth.uid(),
  created_at timestamptz default now()
);
alter table push_tokens enable row level security;
create policy "own tokens" on push_tokens for all to authenticated
  using (user_id = auth.uid()) with check (user_id = auth.uid());
