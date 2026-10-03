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

`Default` keeps the existing Claude model. Installing this change does not switch
any task to a different provider. Stored assessments and past ratings remain intact.
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

Before saving a newly selected model, enter and confirm its current input/output
USD prices per million tokens. Discovery does not invent prices or activate models.
Price tables and task assignments live in `data/settings/assistant.yaml`. All providers
share the existing daily soft spending threshold and usage log. In-flight requests
can exceed the threshold. Non-Claude estimates count cached input at the full input
price; reported reasoning output is included. Prices for long contexts, tiers, and
provider routing can vary: use a conservative configured rate and check invoices.

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
