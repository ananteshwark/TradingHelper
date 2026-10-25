# Private AI inference through Bionic

The dashboard can stay on its public server while every language-model task runs on
an owner's private computer. The computer opens an outbound SSH reverse tunnel;
the server's AI listener stays on `127.0.0.1:18080`, never on a public interface.

The `local` provider uses the OpenAI-compatible Chat Completions API at
`http://127.0.0.1:18080/v1`, authenticated by `STRATA_API_KEY` from the app's private
`.env`. This credential is the private gateway token, separate from cloud DeepSeek
credentials and from any Bionic API key.

Set `IGS_AI_LOCAL_ONLY=1` in `.env` to force all ten assistant tasks through this
provider. It overrides saved cloud assignments, blocks cloud provider transports,
and prevents Claude client creation. An unavailable local model produces an error;
there is no silent cloud fallback. Interactive app calls have no durable offline queue;
the separate GPU research queue is documented in `GPU_LAB.md`.

With local-only on, `IGS_AI_LOCAL_MODEL` in `.env` names the exact Bionic model every task
uses, for example `IGS_AI_LOCAL_MODEL=fingpt-mt-llama-3-8b-lora`; it must be loaded in
Bionic. Unset, tasks use the automatic `strata-auto` choice below. The model must handle
the app's long prompts: an 8k-token context (FinGPT's) can be too short for company
briefs and stock calls, which then fail with an error rather than a cloud fallback.

The logical route `local:strata-auto` resolves from the authenticated live model
list on each request. It prefers DeepSeek and permits Qwen while DeepSeek is being
prepared. An ambiguous or unsupported model list is refused. Explicit local model
IDs require an exact match. Structured JSON and function calls use the existing
chat transport; responses are recorded with the actual model ID and provider
`local`. Local token charges are zero; hardware and electricity are not estimated.

Use a dedicated SSH key and account restricted to remote forwarding of
`127.0.0.1:18080`, with no shell sessions or password login. Do not publish the
forwarded port through the reverse proxy. The private computer can put an
authenticated loopback gateway in front of Bionic's API at `127.0.0.1:5000` (it was
1235 before a Bionic restart; an optional loopback `bionic_url` in the private
`bridge.json` overrides it). The gateway should restrict API paths, validate the loaded
model, and avoid logging prompts or credentials.

The deployed Windows bridge starts at user sign-in, reconnects after network
interruptions, and forwards only to the local Bionic API. Keep the computer awake,
signed in, and connected, with Bionic running and a model loaded. Its files and keys are private
machine configuration, outside the repository. Public dashboard access and stored
results remain available while local inference is offline.

Migration `071_local_ai_provider.sql` extends the usage-log provider constraint.
Apply migrations before enabling local routes. Configure a fallback price of zero
for `local:strata-auto` when saving explicit local task assignments.

## Bionic gateway

`scripts/private_ai_bridge.py` is the versioned gateway source. Install it as
`bridge_runner.py` beside the existing private `bridge.json`, dedicated SSH key and
pinned `known_hosts`. Preserve those private files and their restricted permissions.
The gateway listens on `127.0.0.1:18081` and exposes only authenticated `/v1/models`
and non-streaming `/v1/chat/completions`. It advertises only loaded GGUF LLMs from
Bionic's `/api/v0/models`; downloaded-but-unloaded models and embeddings are excluded.
Inference is serialized; a busy or offline Bionic returns an explicit error.

Bionic requires `response_format.type=json_schema`. The gateway adapts legacy
`json_object` requests using the schema appended by the application's system prompt.
It never reads schema instructions from user or filing text. Native schemas are preserved.
An optional `bionic_api_key` in private `bridge.json` authenticates the upstream API;
the independent `api_token` still authenticates the server-to-gateway connection.

`STRATA_API_KEY` and `strata-auto` are retained as compatibility setting names. They
now address the Bionic gateway, not a Strata process. No Strata endpoint, launch preset,
or cloud fallback is used. Automatic routing still prefers a loaded DeepSeek model,
then a unique loaded Qwen; an explicit model ID requires an exact loaded match.
The app's provider label identifies Bionic. A model must support the workload's context,
structured extraction and function calls; merely loading successfully is not enough.
