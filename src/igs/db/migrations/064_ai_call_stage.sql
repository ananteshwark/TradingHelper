-- Where the stock was in its move when the AI made the call (igs.assistant.calls: before
-- the move, early, late, near a top, falling, no setup), and how the call relates to the
-- screen's growth label (vs_growth). The user invests to own improving businesses before
-- the price catches up and to book profit near the top; earlier calls have neither.
alter table ai_call
    add column stage text check (stage in ('before the move', 'early in the move',
                                           'late in the move', 'near a top', 'falling',
                                           'no setup')),
    add column vs_growth text;
