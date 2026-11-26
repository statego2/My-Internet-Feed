-- My Internet Feed — sync setup for a Supabase project (free tier).
-- Run this in the Supabase SQL Editor after you create the project.
-- Never commit your sync secret to git.

-- 1. Tables
create table if not exists public.app_config (
  key text primary key,
  value text not null
);

create table if not exists public.feed_state (
  video_id text primary key,
  seen boolean not null default false,
  hidden boolean not null default false,
  updated_at timestamptz not null
);

-- 2. Put your long random sync secret here (the same one you paste into the page).
--    Replace REPLACE_WITH_YOUR_SECRET, then run the script (or this line alone later).
insert into public.app_config (key, value)
values ('sync_secret', 'REPLACE_WITH_YOUR_SECRET')
on conflict (key) do update set value = excluded.value;

-- 3. Sync function: checks the secret, merges rows by updated_at (newer wins),
--    and returns the full table. The browser is the only client.
create or replace function public.sync_feed_state(p_secret text, p_rows jsonb)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
  r jsonb;
  vid text;
  v_seen boolean;
  v_hidden boolean;
  v_updated timestamptz;
  expected text;
begin
  select value into expected from public.app_config where key = 'sync_secret';
  if expected is null or p_secret is null or p_secret <> expected then
    raise exception 'unauthorized' using errcode = '42501';
  end if;

  if p_rows is not null then
    for r in select value from jsonb_array_elements(p_rows)
    loop
      vid := r->>'video_id';
      if vid is null or length(vid) = 0 then
        continue;
      end if;
      v_seen := coalesce((r->>'seen')::boolean, false);
      v_hidden := coalesce((r->>'hidden')::boolean, false);
      v_updated := (r->>'updated_at')::timestamptz;
      if v_updated is null then
        continue;
      end if;
      insert into public.feed_state (video_id, seen, hidden, updated_at)
      values (vid, v_seen, v_hidden, v_updated)
      on conflict (video_id) do update
        set seen = excluded.seen,
            hidden = excluded.hidden,
            updated_at = excluded.updated_at
        where excluded.updated_at >= public.feed_state.updated_at;
    end loop;
  end if;

  return coalesce((
    select jsonb_agg(jsonb_build_object(
      'video_id', video_id,
      'seen', seen,
      'hidden', hidden,
      'updated_at', updated_at
    ) order by video_id)
    from public.feed_state
  ), '[]'::jsonb);
end;
$$;

-- 4. Lock down tables. Anon can only call the function, never read the secret
--    or the rows directly.
alter table public.app_config enable row level security;
alter table public.feed_state enable row level security;
-- No policies on purpose: direct table access is denied.

revoke all on table public.app_config from public, anon, authenticated;
revoke all on table public.feed_state from public, anon, authenticated;
grant execute on function public.sync_feed_state(text, jsonb) to anon, authenticated;

-- Optional later change of the secret:
--   update public.app_config set value = 'NEW_SECRET' where key = 'sync_secret';
