# Models by task

Settings → Providers and models by task lets the administrator choose Anthropic,
OpenAI, Google Gemini, DeepSeek, or OpenRouter independently for:

- Ask / read-only research tools
- Company briefs
- AI stock calls
- Broker-call verdicts
- Broker-call extraction
- Stock-news sentiment
- Geopolitical impact
- Announcement notes
- Public filing guidance and order-book extraction

`Default` now uses automatic cost-conscious routing. Admin-selected provider/model
pairs stay pinned. Disable automatic routing to make Default use the configured Claude model. Stored assessments and past ratings remain intact.
Only newly requested work uses the saved assignments. Cached briefs remain cached.
The selected provider receives the same task inputs previously sent to Claude.

Add provider API keys in the admin Settings page. Keys are stored privately in the
server `.env`, never in the model catalog or tracked configuration. Supported names:
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` (or `GOOGLE_API_KEY`),
`DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`. Chat subscriptions do not configure API keys.
The API origins are fixed; task input and keys are never sent to arbitrary URLs.

Click **Refresh available models** after adding a key. `igs-models.timer` also
refreshes catalogs every six hours, independently of the UI. It uses the official
Models APIs, with pagination where provided, and makes no paid inference calls.
New models appear when the provider lists them for the configured account, not
necessarily at the public announcement. A failed refresh retains the last successful
catalog and reports the failure. Models removed upstream disappear from discovery,
but saved assignments remain visible until changed; an unavailable assignment fails
clearly instead of switching providers silently.

Model listing is not a compatibility guarantee. Non-text models are filtered where
identifiable; tasks require text generation, structured JSON, and (for Ask) function
calling. Provider errors remain visible. A newly introduced protocol or unsupported
model capability can require an app update. Anthropic effort options are used for
known adaptive-thinking models; other providers use their default reasoning settings.
OpenRouter offers additional families through its chat-completions API.

Automatic routing filters the account's model catalog by configured credentials, a
reviewed general-purpose model-family policy, structured-output/tool capability,
at least 64k input context and the task's configured output limit. Calls, broker
verdicts, geopolitical assessments, briefs, research and filing guidance require
the reasoning tier; extraction and sentiment allow the economy tier. Among eligible
models, it minimizes estimated cost for an illustrative 8k input / 2k output request.
Each task shows the suggestion and rationale. This is a heuristic, not measured proof
that a model is best at stock analysis. Unknown families and preview/specialist models
require manual selection. New models may be listed without qualifying for auto-routing.
An unavailable/stale catalog falls back to the configured Claude model. A selected
route stays fixed throughout an Assistant instance's tool conversation. Explicit pins
never silently switch providers when unavailable.

The six-hour discovery job checks pricing freshness too and refreshes rates once
seven days have elapsed (retrying failures on subsequent runs). It downloads the public
[LiteLLM reference catalog](https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json)
for direct providers, and OpenRouter's official Models API for OpenRouter. It sends
no keys or task content to these public pricing endpoints. Direct-provider rates are
third-party reference estimates, not independently verified provider quotes. Exact
model IDs are matched; regional/batch variants are not substituted. The Settings page
shows the source, last complete update and errors. Manual refresh updates both catalogs
and prices. Atomic local storage is `data/settings/model_prices.json`.

Fresh automatic reference prices take precedence over configured fallback prices.
After eight days without a successful update for a rate, saved prices are used instead;
automatic routing excludes models without fresh reference metadata. Admins may disable
automatic pricing and enter their own rates. Before pinning a model, save its fallback
input/output prices so a pricing-source outage cannot invalidate the configuration.
Disabling auto-pricing does not disable weekly downloads. All providers share the
existing daily soft spending threshold and usage log. In-flight requests can exceed
that threshold. Cached-input and long-context pricing, special tiers and routing can
vary; estimates are not invoices. Historical usage costs are never rewritten by a
pricing refresh. No paid benchmark or inference calls are made during model/price refresh.

Deployment:

```bash
uv run igs db migrate
scripts/install-schedules.sh
uv run igs models refresh
```

Migration 034 records the provider in `llm_call` and separates filing extraction from
announcement notes. Existing usage rows retain Anthropic as their provider. The CLI,
scheduled jobs, and dashboard read the same settings. Restart long-lived app/API
processes after deploying code changes.

Provider contracts used:
[OpenAI Responses](https://developers.openai.com/api/docs/guides/function-calling),
[Anthropic Models](https://platform.claude.com/docs/en/api/typescript/models),
[Gemini Models](https://ai.google.dev/api/models),
[Gemini function calling](https://ai.google.dev/gemini-api/docs/function-calling),
[DeepSeek API](https://api-docs.deepseek.com/api/create-chat-completion/), and
[OpenRouter Models](https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties).
