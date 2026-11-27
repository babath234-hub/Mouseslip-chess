-- Run this once in the Supabase SQL Editor for Mouseslip Chess.
create table if not exists public.daily_challenge_queue (
    id uuid primary key default gen_random_uuid(),
    created_by uuid not null references public.players(id) on delete cascade,
    creator_username text not null,
    title text not null,
    position jsonb not null,
    goal text not null check (goal in ('Win','Draw','Lose')),
    difficulty integer not null check (difficulty between 1 and 4),
    side_to_move text not null check (side_to_move in ('white','black')),
    status text not null default 'pending' check (status in ('pending','published')),
    created_at timestamptz not null default now(),
    published_date date unique
);
create index if not exists daily_challenge_queue_pending_idx
    on public.daily_challenge_queue(status, created_at);

create table if not exists public.daily_challenge_queue_completions (
    id uuid primary key default gen_random_uuid(),
    challenge_id uuid not null references public.daily_challenge_queue(id) on delete cascade,
    player_id uuid not null references public.players(id) on delete cascade,
    completed_at timestamptz not null default now(),
    unique(challenge_id, player_id)
);

alter table public.daily_challenge_queue enable row level security;
alter table public.daily_challenge_queue_completions enable row level security;
grant all on public.daily_challenge_queue to service_role;
grant all on public.daily_challenge_queue_completions to service_role;
