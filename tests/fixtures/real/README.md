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

`results_AUBANK_2024Q3_standalone.xml`: NSE's downloaded AUBANK standalone
quarter ended 2024-12-31, retrieved from the local immutable raw store on
2026-09-25. Source: https://nsearchives.nseindia.com/corporate/xbrl/BANKING_117651_1360692_24012025062951.xml.
Verified interest + other income, PBT minus tax, and PAT/EPS/share-capital consistency.
The banking 2019 mapping omits NPA ratios pending independent unit verification.

Insider trading (SEBI PIT), captured 2026-09-29:
`insider_trading_2026-04-01_2026-04-07.json` is NSE's older corporates-pit API for
1-7 April 2026, pasted from a browser: 77 of its 219 rows (the first 40 and one of each
other person category / mode / transaction type / security type combination), with
acqNameList cut to their names. `insider_disclosures_2026-09-22_2026-09-29.json` is the
current corporates-pit-gg listing, pasted from a browser: 54 of its 253 rows (the first 44,
every row of the three companies with a revision, and HCLTECH). Both are re-serialised
compactly; values are as served. `insider_xbrl_MAYURUNIQ_20260929.xml` and
`insider_xbrl_HCLTECH_20260925.xml` are two of the listing's XBRL files from nsearchives,
byte-for-byte. The CDN refused most repeat requests for the same file from the cloud, as
above.

Economic Times stock news RSS, captured 2026-09-30
(https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms):
`et_stocks_rss_2026-09-30.xml` keeps 4 of its 50 items (two broker calls, Jefferies on
Molbio and Morgan Stanley on Lenskart; a block deal that is not a call; an order win),
otherwise as served. Used by tests/test_brokers.py.

Moneycontrol brokerage recommendations, captured 2026-09-30 from its last RSS feed
(https://www.moneycontrol.com/rss/brokeragerecos.xml, which stopped on 23 April 2024):
`moneycontrol_recos_2024-04-23.txt` has its first 6 items as title, date and description,
the headline format the app reads from text pasted from Moneycontrol's pages. Used by
tests/test_brokers.py.

Moneycontrol news sitemap, captured 2026-09-30
(https://www.moneycontrol.com/news/news-sitemap.xml, listed in its robots.txt):
`moneycontrol_news_sitemap_2026-09-30.xml` keeps 10 of its 1,000 `<url>` entries byte for
byte, with the sitemap's own opening and closing. From the stocks section: three call
headlines ("Neutral ICICI Lombard; target of Rs 1700: Motilal Oswal" and two more), a
rating story (Nomura on Allied Blenders), an order win and an IPO note. From the markets
section: a rating story (JPMorgan on Coforge) and an FII-flows story. And an
entertainment and a world story that the section filter drops. Used by tests/test_news.py
and tests/test_brokers.py.
