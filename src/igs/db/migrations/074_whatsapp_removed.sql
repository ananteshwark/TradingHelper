-- WhatsApp messages were removed at the owner's request (2026-10-27): what was still queued
-- for WhatsApp is cancelled, so it doesn't wait in the outbox for good. Sent rows stay as
-- the record; the channel stays allowed in the constraint for them.
update alert_outbox set status = 'cancelled', last_error = 'WhatsApp messages were removed'
where channel = 'whatsapp' and status in ('pending', 'failed');
