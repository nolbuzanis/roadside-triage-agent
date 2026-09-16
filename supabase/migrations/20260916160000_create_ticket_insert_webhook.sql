-- Create a webhook trigger that fires after a new breakdown_ticket is inserted.
-- Uses pg_net (available by default in Supabase) to POST the row payload to
-- the FastAPI dispatcher notification endpoint.  The call is non-blocking from
-- the database's perspective so notification failures never roll back the
-- insert.
--
-- WEBHOOK_URL must be set to the deployed FastAPI base URL before running
-- this migration against a remote Supabase project:
--
--   export WEBHOOK_URL=https://<your-deployed-url>/api/v1/webhooks/ticket-created
--   supabase db push

create or replace function public.notify_ticket_created()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  perform net.http_post(
    url    := coalesce(current_setting('app.settings.webhook_url', true), 'http://host.docker.internal:8000/api/v1/webhooks/ticket-created'),
    headers := '{"Content-Type": "application/json"}'::jsonb,
    body   := jsonb_build_object(
      'id',              NEW.id,
      'call_id',         NEW.call_id,
      'caller_phone',    NEW.caller_phone,
      'location',        NEW.location,
      'vehicle',         NEW.vehicle,
      'issue',           NEW.issue,
      'status',          NEW.status,
      'created_at',      NEW.created_at
    )
  );
  return NEW;
end;
$$;

create trigger ticket_insert_notification
  after insert on public.breakdown_tickets
  for each row
  execute function public.notify_ticket_created();
