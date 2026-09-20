-- Per-user redesign: accounts, plus user-scoped portfolio, chat log and journal.
--
-- Replaces the single global `app_state` row as the source of truth. `app_state`
-- is deliberately left in place as a rollback safety net — nothing here drops it.
--
-- Idempotent so it is safe to re-run on any branch or fresh environment.
-- RLS is on with no policies everywhere, matching app_state: the anon/public key
-- has zero access and only the service_role key (used server-side) can read/write.

create extension if not exists pgcrypto;

create table if not exists public.users (
  id            uuid primary key default gen_random_uuid(),
  email         text unique not null,
  password_hash text not null,
  display_name  text,
  is_owner      boolean not null default false,
  created_at    timestamptz not null default now(),
  last_login_at timestamptz
);

-- Same jsonb shape app_state used ({holdings, watchlist}), now keyed by user.
create table if not exists public.user_state (
  user_id    uuid primary key references public.users(id) on delete cascade,
  data       jsonb not null default '{"holdings":[],"watchlist":[]}'::jsonb,
  updated_at timestamptz not null default now()
);

-- Write-only chat log: every user/assistant turn, kept for the record. Nothing
-- reads it back into the prompt — history still comes from the browser.
create table if not exists public.chat_messages (
  id         bigserial primary key,
  user_id    uuid not null references public.users(id) on delete cascade,
  role       text not null check (role in ('user','assistant')),
  content    text not null,
  created_at timestamptz not null default now()
);
create index if not exists chat_messages_user_time
  on public.chat_messages (user_id, created_at desc);

-- Journal moves off the ephemeral local journal.json (wiped on every Vercel cold
-- start). Entry body stays free-form jsonb so add_journal_entry()'s shape works
-- unchanged.
create table if not exists public.journal_entries (
  id         text primary key,
  user_id    uuid not null references public.users(id) on delete cascade,
  data       jsonb not null,
  created_at timestamptz not null default now()
);
create index if not exists journal_entries_user_time
  on public.journal_entries (user_id, created_at desc);

alter table public.users           enable row level security;
alter table public.user_state      enable row level security;
alter table public.chat_messages   enable row level security;
alter table public.journal_entries enable row level security;
