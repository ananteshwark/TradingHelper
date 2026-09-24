Real payloads captured from NSE on 2026-09-23 (public data), used as parser
regression fixtures by tests/test_real_payloads.py. Large files are trimmed:
the three JSON listings to their first 50 records and EQUITY_L / MTO /
sec_bhavdata_full to their first 300 lines. Everything else is byte-for-byte as
served.

Integrated Filing (Financials) XBRL, captured 2026-09-24 from nsearchives
(in-capmkt-ent-2026-01-31.xsd), byte-for-byte:
results_integrated_PNCINFRA_2026Q4_consolidated.xml (audited Q4 FY26 with the
year, balance sheet and cash flow), results_integrated_ARIHANTCAP_2026Q4_
consolidated_nbfc.xml (NBFC form) and results_integrated_ANNU_2026Q1_standalone
.xml (unaudited first quarter). While capturing them, NSE's CDN refused a second
request for a document URL already fetched from the same address (403 "Access
Denied") while first requests for other documents succeeded: fetch each
document once and keep it; do not rely on re-downloading.
