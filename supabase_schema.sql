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

-- Daily per-user usage counter backing the shared Gemini-key rate limit
create table public.usage_counters (
  user_id          uuid not null references auth.users(id) on delete cascade,
  usage_date       date not null default (now() at time zone 'utc')::date,
  generation_count int not null default 0,
  primary key (user_id, usage_date)
);

create or replace function public.increment_usage(p_user_id uuid, p_date date)
returns int language plpgsql security definer as $$
declare new_count int;
begin
  insert into public.usage_counters (user_id, usage_date, generation_count)
  values (p_user_id, p_date, 1)
  on conflict (user_id, usage_date)
  do update set generation_count = usage_counters.generation_count + 1
  returning generation_count into new_count;
  return new_count;
end;
$$;

alter table public.resumes enable row level security;
alter table public.generation_jobs enable row level security;
alter table public.usage_counters enable row level security;
create policy "own row" on public.resumes for all using (auth.uid() = user_id);
create policy "own row" on public.generation_jobs for all using (auth.uid() = user_id);
create policy "own row" on public.usage_counters for all using (auth.uid() = user_id);

-- ---------------------------------------------------------------------------
-- Migrations for projects created before a feature landed. Safe to re-run.
-- ---------------------------------------------------------------------------

-- Cover letter generation (adds a .docx alongside every generated resume PDF)
alter table public.generation_jobs
  add column if not exists cover_letter_storage_path text,
  add column if not exists cover_letter_error text;
