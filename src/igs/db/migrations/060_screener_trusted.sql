-- Screener.in exports are trusted like exchange filings (the owner's decision): an export
-- whose reporting basis its own figures can't confirm is still used in scoring, as the
-- basis its figures come closest to or, with nothing to compare, the company's own basis
-- (igs.screener.verify). The exchange's figures still take precedence wherever both have
-- one. basis_rule says how an export's basis was set.
alter table screener_export_context
    add column basis_rule text not null default 'verified'
        check (basis_rule in ('verified', 'closest', 'assumed'));
