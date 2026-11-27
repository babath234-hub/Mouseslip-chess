-- Run once in the Supabase SQL Editor.
-- Tracks how many enemy kings each logged-in player has captured with each piece.
create table if not exists public.player_killer_progress (
    player_id uuid not null references public.players(id) on delete cascade,
    piece_type text not null check (piece_type in ('rook','knight','bishop','queen','pawn','king')),
    king_captures integer not null default 0 check (king_captures >= 0),
    updated_at timestamptz not null default now(),
    primary key (player_id, piece_type)
);

alter table public.player_killer_progress enable row level security;
grant all on public.player_killer_progress to service_role;


-- Stores only achievements that a logged-in player has actually earned.
-- The website never inserts locked achievements into this table.
create table if not exists public.player_achievement_unlocks (
    id uuid primary key default gen_random_uuid(),
    player_id uuid not null references public.players(id) on delete cascade,
    achievement_key text not null,
    unlocked_at timestamptz not null default now(),
    unique (player_id, achievement_key)
);

alter table public.player_achievement_unlocks enable row level security;
grant all on public.player_achievement_unlocks to service_role;
