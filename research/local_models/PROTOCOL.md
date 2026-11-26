# Study E: training FinGPT and Qwen for the app's AI tasks

Written 2026-10-26, before any label was computed or any model trained for this study. Its
SHA-256 is in `PROTOCOL.sha256`. Later changes are dated amendments at the end, before the
result they could affect.

## Question

The owner wants two local models in Bionic: FinGPT for short financial reads, Qwen for the
long tasks. Can each, fine-tuned on this app's own data, do its tasks at least as well as
the cloud model whose answers the app stored, so the app can use it instead?

## Split (fixed by prompt size, docs/LOCAL_AI.md)

- **FinGPT** (Llama 3 8B, 8,192-token context): stock-news sentiment (`news_tone`),
  broker-call extraction (`brokers`), geopolitical impact (`geopolitical`). Their prompts
  are under 3,500 tokens.
- **Qwen 2.5 7B Instruct** (32k context): stock calls (`call`), trained now; the other
  long tasks use the same trained model untrained for them.

## Data (the server's database, as of 2026-10-26)

Prompts are rebuilt exactly as the app builds them from the stored inputs (articles,
news and companies, call inputs) with each task's current system prompt; answers from
the stored results. A batch of one article per example. Only results stored by a cloud
model are used, never by a local one.

- `news_tone`: every article whose tone was read (`tone_read_at`), with its stored tones;
  an article with none gives an empty list.
- `brokers`: every candidate article read for calls (`read_at`), with its stored calls;
  none gives an empty list. `nse_symbol` is "" (not stored); `report_date` is the call's
  date when it differs from the article's, else "".
- `geopolitical`: every article with stored assessments, all its companies.
- `call`: calls in the current output schema (as `research/finetune`'s export).

## Outcome labels (sentiment and geopolitical only)

A tone or impact on a company is judged by the company's excess return over the Nifty
500 from the last close at or before publication (a story after 15:30 IST starts from
that day's close) to the close three sessions later, from `price_eod` and `index_price`.
Band 0.5%. A tone of at least +0.2 **agrees** when the excess is above +0.5%, one of at
most -0.2 when it is below -0.5%; a tone between them when the excess is within the
band. Rows without the prices are unlabelled.

## Training sets (outcome-filtered where labels exist)

- `news_tone`: an article's stored items minus those that disagree; an article whose items
  all disagree is dropped; articles with no items are kept (the model must learn to return
  nothing). Unlabelled items are kept.
- `geopolitical`: the app requires every supplied company, so items can't be dropped. An
  article is kept when at least 60% of its labelled items agree, or none is labelled.
- `brokers`, `call`: imitation (no outcome exists yet for calls; extraction has none).

## Split by publication date

Training: published before 2026-10-13. Evaluation: 2026-10-13 to 2026-10-20 (the last
date whose three-session outcome exists by 2026-10-23); calls: made on or after
2026-10-20. Nothing from the evaluation dates is trained on.

## Models and training (fixed now)

- **FinGPT:** `NousResearch/Meta-Llama-3-8B` (the Meta weights, ungated) with
  `FinGPT/fingpt-mt_llama3-8b_lora` merged in, then QLoRA (rank 16, alpha 32, all linear
  layers, learning rate 1e-4, 2 epochs, max length 8,192, loss on the answer only), Llama 3
  chat template. One model for its three tasks.
- **Qwen:** `Qwen/Qwen2.5-7B-Instruct`, `igs.finetune.train` settings, max length 24,576.
- Merged, converted to GGUF (q8_0) with llama.cpp, loaded in Bionic.

## Evaluation (held-out dates, each model and the cloud answers on the same inputs)

- All tasks: share of answers that are valid JSON passing the app's own checks (schema,
  quote or evidence in the article, every geopolitical company covered).
- `news_tone`, `geopolitical`: agreement rate of the model's non-neutral items with the
  outcome, against the cloud's on the same articles; and how often it finds news where the
  cloud found none and the reverse.
- `brokers`: precision and recall of (company, broker, stance) against the cloud's calls.
- `call`: share of actions equal to the cloud's.

## Use rule (decided now)

A task moves to its trained local model only if, on the evaluation dates:
1. at least 95% of answers pass the app's checks; and
2. `news_tone` and `geopolitical`: outcome agreement no more than 2 percentage points
   below the cloud's; `brokers`: precision and recall both at least 0.8 against the cloud;
   `call`: actions equal to the cloud's at least 70% of the time.

Otherwise the task keeps its current model and the result is reported. With about a month
of data, every figure is noisy; a pass is a reason to try the model, not proof of skill.
