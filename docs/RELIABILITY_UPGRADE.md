# Reliability upgrade

## Upgrade and recover stored documents

Stop/restart any ingestion process that was started before the upgrade so that all
writers use the shared ingestion lock. Apply the additive migrations:

```bash
uv run igs db migrate
uv run igs ingest replay-documents financial_results
uv run igs ingest replay-documents shareholding
```

Replay reads downloaded XML from the raw store. It does not fetch anything or truncate
financial tables. Documents that already have a filing are excluded. Each attempt stores
its status, parser/mapping fingerprint, attempts, row count, error and processing time.
Legacy HTTP-200 downloads without processing records are included when they have no filing.
A failed document does not stop the remaining replay batch. `--limit N` limits attempts.
Fix a failed prerequisite and rerun the same command. Use `-v` for detailed data-quality notes.
Normal downloads now print processed/total progress and per-document outcomes.

The Data quality page, and the initial no-score page, show processing totals and recent
failures. Totals cover tracked processing attempts, not all historic successful downloads.

## Alert delivery

Insider disclosures, announcement notes and future alert categories appear in digests.
Configured external channels use a durable outbox. A delivery failure retains the alert,
with at most five automatic attempts and increasing five-minute retry intervals on later
alert runs. Each channel is independent; a successful channel is not resent on retry.
No credentials still means file-only operation. External notifications have at-least-once
semantics: remote acceptance followed by a crash before local acknowledgement can duplicate
a notification. No live notifications were used to test this change.

After fixing credentials, failed rows remain inspectable in `alert_outbox`. To explicitly
retry exhausted attempts, reset only the affected channel/alert rows' attempts and due time
with database administration tooling; automatic retries remain bounded.

## Coverage and historical correctness

- AUBANK's exact `banking_entry_point_2019-09-30.xsd` schema is supported, backed by its real
  December 2024 filing. Profit, tax, interest, EPS and capital identities are tested.
  Its NPA percentages remain unavailable pending independent unit verification.
- Configured SME/equity series propagate to scoring and backtest dataset loading.
- Historical stock pages resolve the issuer stored in the run. Announcement joins respect
  identifier validity dates.
- Saved screens validate filter types, including legacy screens when executed. Invalid
  input returns 422; absent optional assistant dependencies return 503.
- Backtest forward paths retain the primary security selected using the signal-date view,
  so a later security line cannot silently replace the entry security.

## Coordination and reproducibility

Manual ingestion, replay, imports and master rebuilds share the sync/daily advisory lock.
A busy manual job exits with a retry message. This coordination covers the supplied CLI;
custom scripts calling internal loader functions must coordinate their own work.
Score runs store source/config, dependency-lock and mapping hashes, commit ID and raw-data
watermarks. New non-autocommit scoring transactions use repeatable-read isolation; callers
with existing transactions own their isolation settings. Validation artifacts record a
fingerprint and the latest realized forward exit date. Legacy, incompatible or future-trained
artifacts are treated as unvalidated; rerun `igs gate run` and the backtest after upgrading.
The assistant spending setting is explicitly labeled a soft threshold, not a hard cap.

## Performance and backtest assessment

`performance-baseline.json` records a read-only local loader sample: 225,450 price rows plus
68,887 financial facts loaded in 2.46 seconds, with 508 MiB peak process RSS. This is one
warm/cold-cache-uncontrolled observation, not a service-level guarantee or a full-history
scoring benchmark. The memory result supports investigating batched database-to-Polars
loading before adding aggressive UI caches. No speculative indexes were added.

The existing stopped-trading baseline uses the last observed close. The report flags that
condition, but the price is not evidence of an executable exit. Recovery haircuts, delayed
fills and total-return benchmark coverage need verified market data and explicit modeling
assumptions before they should change reported returns. The security-identity defect is
fixed; speculative recovery assumptions are not silently introduced.

UI pooling, additional caching and broad page refactoring remain optional later work rather
than prerequisites for this reliability upgrade. The new minimal-install CI job and targeted
failure-path tests cover the concrete regressions identified in the review.
