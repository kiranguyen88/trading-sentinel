-- Single-row key/value store for the app's portfolio + watchlist snapshot.
-- Idempotent so it is safe to re-run on any branch or fresh environment.
create table if not exists public.app_state (
  id text primary key,
  data jsonb not null,
  updated_at timestamptz not null default now()
);

-- RLS on with no policies => anon/public key has zero access. Only the
-- service_role key (used server-side, bypasses RLS) can read/write.
alter table public.app_state enable row level security;
