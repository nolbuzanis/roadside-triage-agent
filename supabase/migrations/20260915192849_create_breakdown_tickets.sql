create table breakdown_tickets (
  id uuid primary key default gen_random_uuid(),
  call_id text not null unique,
  tool_call_id text,
  caller_phone text,
  location text not null,
  vehicle text not null,
  issue text not null,
  status text not null default 'pending',
  hazard_detected boolean not null default false,
  hazard_reason text,
  notification_status text not null default 'pending',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table breakdown_tickets enable row level security;

create policy "Anonymous cannot access tickets"
  on breakdown_tickets
  for all
  using (false);
