# Private AI inference through Strata

The dashboard can stay on its public server while every language-model task runs on
an owner's private computer. The computer opens an outbound SSH reverse tunnel;
the server's AI listener stays on `127.0.0.1:18080`, never on a public interface.

The `local` provider uses the OpenAI-compatible Chat Completions API at
`http://127.0.0.1:18080/v1`, authenticated by `STRATA_API_KEY` from the app's private
`.env`. This credential is the private gateway token, separate from cloud DeepSeek
credentials and from Strata's own local API key.

Set `IGS_AI_LOCAL_ONLY=1` in `.env` to force all ten assistant tasks through this
provider. It overrides saved cloud assignments, blocks cloud provider transports,
and prevents Claude client creation. An unavailable local model produces an error;
there is no silent cloud fallback or durable offline inference queue.

The logical route `local:strata-auto` resolves from the authenticated live model
list on each request. It prefers DeepSeek and permits Qwen while DeepSeek is being
prepared. An ambiguous or unsupported model list is refused. Explicit local model
IDs require an exact match. Structured JSON and function calls use the existing
chat transport; responses are recorded with the actual model ID and provider
`local`. Local token charges are zero; hardware and electricity are not estimated.

Use a dedicated SSH key and account restricted to remote forwarding of
`127.0.0.1:18080`, with no shell sessions or password login. Do not publish the
forwarded port through the reverse proxy. The private computer can put an
authenticated loopback gateway in front of Strata so Strata key changes remain
local. The gateway should restrict API paths, validate the loaded model, and avoid
logging prompts or credentials.

The deployed Windows bridge starts at user sign-in, reconnects after network
interruptions, and reads the active Strata launch preset. Keep the computer awake,
signed in, and connected, with Strata running. Its files and keys are private
machine configuration, outside the repository. Public dashboard access and stored
results remain available while local inference is offline.

Migration `071_local_ai_provider.sql` extends the usage-log provider constraint.
Apply migrations before enabling local routes. Configure a fallback price of zero
for `local:strata-auto` when saving explicit local task assignments.
