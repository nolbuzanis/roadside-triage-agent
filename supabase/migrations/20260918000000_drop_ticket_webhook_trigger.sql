-- Drop the ticket-created webhook trigger and function.
--
-- Notifications are now dispatched directly by the application after
-- inserting a ticket, making the database trigger unnecessary.

drop trigger if exists ticket_insert_notification on public.breakdown_tickets;
drop function if exists public.notify_ticket_created();
