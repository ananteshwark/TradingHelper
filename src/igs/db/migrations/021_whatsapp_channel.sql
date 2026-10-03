-- WhatsApp as an alert channel: one message per AI buy / sell call (igs.alerts.whatsapp).
alter table alert_outbox drop constraint alert_outbox_channel_check;
alter table alert_outbox add constraint alert_outbox_channel_check
    check (channel in ('email', 'telegram', 'whatsapp'));
