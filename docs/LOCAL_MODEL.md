# Local model for AI stock calls

Fine-tunes Qwen 2.5 7B Instruct on the app's own AI stock calls, on a local NVIDIA GPU
(tested design target: RTX 5090 Laptop, 24 GB), and serves it through Ollama. It is a
research result: nothing routes app tasks to it. Use it in place of a cloud provider only
if it beats the cloud model on held-out calls by a margin the evaluation size supports.

## What it learns from

`ai_call` stores, with every call, the inputs the model saw (`inputs`) and its answer.
`igs finetune-export` keeps calls in the current output schema whose own horizon (1, 3, 6
or 12 months) has been measured, using the same outcome as the track record: the excess
return over the Nifty 500 from the call's entry.

- **Training** keeps only calls the outcome agreed with (filtered fine-tuning): a buy that
  beat the index by more than `--band` (default 2%), a sell that lagged it by more, a hold
  within it. The model learns to imitate the cloud model's calls that proved right.
- **Evaluation** keeps every call made on or after `--cutoff`, right or wrong.
- **Embargo**: a training call must have been made `--embargo-days` (default 365) before
  the cutoff, so no training outcome overlaps the evaluation period.

The previous call and last buy shown at the time are not stored, so the rebuilt prompt
omits them. With few matured calls the model can memorise; check `summary.json` first.

## Steps

1. **Connect to the server database.** Open a tunnel so the server's Postgres is never
   exposed on the network, then point the app at it:

   ```bash
   ssh -N -L 15432:localhost:5432 user@your-server
   ```

   ```bash
   export IGS_DATABASE_URL=postgresql://igs:PASSWORD@localhost:15432/igs
   ```

2. **Export** (training data stays in `data/`, which git ignores):

   ```bash
   uv run igs finetune-export --cutoff 2026-04-01
   ```

3. **Train** (installs PyTorch for CUDA 12.8+, needed by RTX 50-series GPUs):

   ```bash
   uv pip install torch --index-url https://download.pytorch.org/whl/cu128
   uv pip install -r requirements-finetune.txt
   uv run --no-sync python -m igs.finetune.train --data data/finetune
   ```

   Examples longer than `--max-length` tokens (default 16384) are dropped and counted,
   never truncated. The first run downloads the base model (~15 GB) from Hugging Face.

4. **Convert to GGUF and import into Ollama.** Ollama 0.13+ cannot import Qwen2
   safetensors directly (its importer only handles MLX architectures), so convert with
   llama.cpp's converter first. GGUF also loads in LM Studio and most local runtimes.

   ```bash
   git clone --depth 1 https://github.com/ggml-org/llama.cpp.git ../llama.cpp
   ```

   ```bash
   uv pip install ../llama.cpp/gguf-py sentencepiece
   ```

   ```bash
   uv run --no-sync python ../llama.cpp/convert_hf_to_gguf.py data/finetune/model --outtype q8_0 --outfile data/finetune/model/igs-call.gguf
   ```

   ```bash
   ollama create igs-call -f data/finetune/model/Modelfile
   ```

   `q8_0` keeps a 7B model at about 8 GB, comfortable on a 24 GB GPU.

5. **Evaluate** against the cloud model on the same held-out calls:

   ```bash
   uv run igs finetune-eval --model igs-call
   ```

   `eval_report.json` gives, for both, the share of buy/sell calls that proved right
   against the Nifty 500, the mean excess of buys, and invalid answers. The base model
   (`ollama pull qwen2.5:7b`) is a useful second baseline.

   To evaluate a model loaded in Strata instead (the app's `local` provider,
   [LOCAL_AI.md](LOCAL_AI.md)), point at Strata's OpenAI-compatible URL on this computer:

   ```bash
   uv run igs finetune-eval --base-url http://127.0.0.1:PORT/v1 --model MODEL_ID --key-env STRATA_API_KEY
   ```

## Using it in the app

Only after it beats the cloud model on held-out calls. Load the merged model
(`data/finetune/model`) in Strata, then assign it to the AI calls task as an explicit
`local` model ID. Do not rely on `local:strata-auto`: it picks a model whose ID contains
"deepseek", else "qwen", so a fine-tuned model may or may not be chosen depending on what
else Strata has loaded.

## Limits

- Calls overlap in time and share market moves, so evaluation calls are not independent;
  a few dozen calls cannot establish an edge.
- Retraining with a cutoff chosen after seeing evaluation results makes that period
  in-sample. Fix the cutoff before looking.
- Training imitates the cloud model's right calls; it cannot learn information the
  inputs never contained.
