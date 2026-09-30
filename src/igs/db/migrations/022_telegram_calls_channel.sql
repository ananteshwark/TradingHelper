-- The detailed message for each new AI buy / sell call on Telegram (igs.alerts.delivery),
-- as a channel of its own beside the Telegram digest.
alter table alert_outbox drop constraint alert_outbox_channel_check;
alter table alert_outbox add constraint alert_outbox_channel_check
    check (channel in ('email', 'telegram', 'whatsapp', 'telegram_calls'));
