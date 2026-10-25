# Measure local GPU analytics

This research lane uses the existing private Bionic gateway and the production forward
filing prompt. It stores experiments in a separate local SQLite database. It does not
write company scores, AI calls, trading permissions, or orders. Existing fine-tuning
tools in [LOCAL_MODEL.md](LOCAL_MODEL.md) remain available as a separate experiment.

## Run

Create a UTF-8 JSONL document file. Each line requires `document_id`, `source_url`,
`published_at`, `received_at` (timezone-aware ISO timestamps), and `text` (up to 40,000
characters). Retain the original file unchanged. Use the exact model ID from the
authenticated gateway `/v1/models`, rather than `strata-auto`, so changing the loaded
model cannot silently change an experiment. Set `STRATA_API_KEY` in the process
environment to the private gateway token; never put it in commands or tracked files.

```bash
uv run python -m igs.gpu_lab enqueue --documents data/gpu-lab/documents.jsonl --model EXACT_ID --protocol research/gpu_lab/PROTOCOL.md
uv run python -m igs.gpu_lab work --location computer --limit 5
uv run python -m igs.gpu_lab report --out data/gpu-lab/report.json
```

Use `--db PATH` before the subcommand for an alternative queue. On the Linux server use
`--location server`; both endpoints are fixed loopback addresses, with no cloud fallback.
The worker on this computer needs the existing bridge task and Bionic running with a model loaded. Research
requests share its single inference slot with the app. Busy/offline jobs remain pending,
with 60/120-second backoff and a three-attempt limit. Reinvoke `work` to resume. No
background scheduler is installed. `--priority 1` runs before the default priority 50.
Multiple workers sharing one database cannot claim concurrent research requests.

A killed process leaves a 15-minute lease; after expiry another invocation recovers it.
Failed jobs remain visible in reports. Re-enqueuing an identical experiment does not
reset its failures or rerun cached results. A deliberate new trial must change the
protocol and retains the original attempt. Keep the SQLite file and its WAL together
while open, or close the worker before copying. Credentials are never stored in it.
It does contain public filing text and model answers, so keep it outside Git.

## Review

Reports include source text, both raw and validated claims, timing and failure counts.
Validation only checks schema, quote/numeric presence and basic date rules. It does not
prove units, scope, attribution or truth. Accuracy is null until labels are supplied.
Prepare a JSON object keyed by document_id with `text_sha256` and `items` (the same
Claim schema as `igs.forward`, including quote and confidence). Then:

```bash
uv run python -m igs.gpu_lab report --labels data/gpu-lab/labels.json --out data/gpu-lab/reviewed.json
```

Use one frozen document/protocol cohort per database for a model comparison; enqueue
each candidate into it with its exact model ID. The source/model/prompt/protocol cache
key prevents reuse across different experimental conditions. Do not combine duplicate
documents from different protocols when interpreting aggregate accuracy.

See [the pilot protocol](../research/gpu_lab/PROTOCOL.md) for the sampling rule and
the separate prospective investment evaluation required before using a new feature.
