-- Run this once in the Supabase SQL Editor for a new project.
-- Also create two private Storage buckets via the dashboard: "photo" and "resume-pdf".

-- Replaces the fixed data/resume.md file -- one row per user
create table public.resumes (
  user_id            uuid primary key references auth.users(id) on delete cascade,
  markdown_text      text not null default '',
  photo_storage_path text,
  updated_at         timestamptz not null default now()
);

-- Job/progress state, readable/writable by any Cloud Run instance
create table public.generation_jobs (
  id                         uuid primary key default gen_random_uuid(),
  user_id                    uuid not null references auth.users(id) on delete cascade,
  status                     text not null default 'running' check (status in ('running','done','error')),
  stage                      text,
  percent                    int not null default 0,
  pdf_storage_path           text,
  cover_letter_storage_path  text,
  cover_letter_error         text,
  error                      text,
  error_detail               text,
  created_at                 timestamptz not null default now(),
  updated_at                 timestamptz not null default now()
);
create index on public.generation_jobs (user_id, created_at desc);

-- One row per generation attempt, backing the sliding-window rate limit on the shared Gemini key.
--
-- A row per event rather than a per-day counter, for two reasons. It's the only shape that can
-- express "no more than N in any rolling window" -- a counter can only express calendar buckets,
-- which allow a burst straddling the boundary (5 at 04:59 and 5 more at 05:01). And it makes
-- refunding a failed generation a plain DELETE of that exact row, so a double refund is a no-op
-- rather than something that has to be guarded against handing out free generations.
create table public.generation_events (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);
create index on public.generation_events (user_id, created_at desc);

-- Check-and-charge in a single atomic call. Returns the new event id, or NULL when the caller is
-- already at the limit (which the app turns into a 429).
create or replace function public.claim_generation_slot(
  p_user_id uuid, p_limit int, p_window interval
) returns uuid language plpgsql security definer as $$
declare used int; new_id uuid;
begin
  -- Serialise per user. Without this, two concurrent requests can both count N-1 and both insert,
  -- letting the user exceed the limit -- the classic check-then-charge race.
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  select count(*) into used
    from public.generation_events
   where user_id = p_user_id
     and created_at > now() - p_window;

  if used >= p_limit then
    return null;
  end if;

  insert into public.generation_events (user_id) values (p_user_id) returning id into new_id;
  return new_id;
end;
$$;

alter table public.resumes enable row level security;
alter table public.generation_jobs enable row level security;
alter table public.generation_events enable row level security;
create policy "own row" on public.resumes for all using (auth.uid() = user_id);
create policy "own row" on public.generation_jobs for all using (auth.uid() = user_id);
create policy "own row" on public.generation_events for all using (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- Migrations for projects created before a feature landed. Safe to re-run.
-- ---------------------------------------------------------------------------

-- Cover letter generation (adds a .docx alongside every generated resume PDF)
alter table public.generation_jobs
  add column if not exists cover_letter_storage_path text,
  add column if not exists cover_letter_error text;

-- Sliding-window rate limit, replacing the per-day usage_counters table.
--
-- Run this block on an existing project. It is written to be safe to re-run, and safe to run on a
-- fresh project where the statements above already created everything.
create table if not exists public.generation_events (
  id         uuid primary key default gen_random_uuid(),
  user_id    uuid not null references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);
create index if not exists generation_events_user_id_created_at_idx
  on public.generation_events (user_id, created_at desc);

alter table public.generation_events enable row level security;
do $$
begin
  if not exists (
    select 1 from pg_policies
     where schemaname = 'public' and tablename = 'generation_events' and policyname = 'own row'
  ) then
    create policy "own row" on public.generation_events for all using (auth.uid() = user_id);
  end if;
end
$$;

create or replace function public.claim_generation_slot(
  p_user_id uuid, p_limit int, p_window interval
) returns uuid language plpgsql security definer as $$
declare used int; new_id uuid;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));

  select count(*) into used
    from public.generation_events
   where user_id = p_user_id
     and created_at > now() - p_window;

  if used >= p_limit then
    return null;
  end if;

  insert into public.generation_events (user_id) values (p_user_id) returning id into new_id;
  return new_id;
end;
$$;

-- The old per-day counter and its RPC are no longer read or written by the app.
drop function if exists public.increment_usage(uuid, date);
drop table if exists public.usage_counters cascade;
