-- Add X-Webhook-Secret header to the ticket-created webhook trigger.
--
-- The secret must be set as a Supabase database setting before this
-- migration takes effect:
--
--   ALTER DATABASE SET "app.settings.webhook_secret" = '<your-secret>';
--
-- This must match the WEBHOOK_SECRET env var in Cloud Run.

create or replace function public.notify_ticket_created()
returns trigger
language plpgsql
security definer
set search_path = public
as $$
begin
  perform net.http_post(
    url    := coalesce(current_setting('app.settings.webhook_url', true), 'http://host.docker.internal:8000/api/v1/webhooks/ticket-created'),
    headers := jsonb_build_object(
      'Content-Type',      'application/json',
      'X-Webhook-Secret',  coalesce(current_setting('app.settings.webhook_secret', true), '')
    ),
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
