"""QLoRA fine-tuning on a local NVIDIA GPU, then merge and write an Ollama Modelfile.

Needs the packages in requirements-finetune.txt (torch with CUDA, transformers, peft, trl,
bitsandbytes). An RTX 50-series GPU needs a PyTorch build for CUDA 12.8 or later.

    uv pip install -r requirements-finetune.txt
    uv run --no-sync python -m igs.finetune.train --data data/finetune
    # convert to GGUF (docs/LOCAL_MODEL.md), then
    ollama create igs-call -f data/finetune/model/Modelfile
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

BASE = "Qwen/Qwen2.5-7B-Instruct"


def records(path: Path) -> list[dict]:
    """Prompt/completion rows, so loss is computed on the assistant's call only."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        item = json.loads(line)
        *prompt, completion = item["messages"]
        if completion["role"] != "assistant":
            raise ValueError("training rows must end with the assistant's call")
        rows.append({"prompt": prompt, "completion": [completion]})
    return rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--data", type=Path, default=Path("data/finetune"))
    p.add_argument("--base", default=BASE)
    p.add_argument("--max-length", type=int, default=16384,
                   help="longer examples are dropped (and counted), never truncated")
    p.add_argument("--epochs", type=float, default=2)
    p.add_argument("--lr", type=float, default=1e-4)
    p.add_argument("--rank", type=int, default=16)
    p.add_argument("--grad-accum", type=int, default=8)
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args(argv)

    import torch
    from datasets import Dataset
    from peft import LoraConfig, PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from trl import SFTConfig, SFTTrainer

    if not torch.cuda.is_available():
        raise SystemExit("No CUDA GPU visible to PyTorch; install a CUDA build of torch")
    tok = AutoTokenizer.from_pretrained(args.base)
    rows = records(args.data / "train.jsonl")
    if not rows:
        raise SystemExit("train.jsonl is empty; export more matured calls first")

    def length(r):
        return len(tok.apply_chat_template(r["prompt"] + r["completion"], tokenize=True))
    kept = [r for r in rows if length(r) <= args.max_length]
    print(json.dumps({"examples": len(rows), "kept": len(kept),
                      "dropped_too_long": len(rows) - len(kept)}), flush=True)
    if not kept:
        raise SystemExit("every example exceeds --max-length")

    quant = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                               bnb_4bit_compute_dtype=torch.bfloat16,
                               bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(args.base, quantization_config=quant,
                                                 dtype=torch.bfloat16,
                                                 attn_implementation="sdpa")
    lora = LoraConfig(r=args.rank, lora_alpha=2 * args.rank, lora_dropout=0.05,
                      target_modules="all-linear", task_type="CAUSAL_LM")
    adapter = args.data / "adapter"
    config = SFTConfig(output_dir=str(adapter), num_train_epochs=args.epochs,
                       learning_rate=args.lr, per_device_train_batch_size=1,
                       gradient_accumulation_steps=args.grad_accum,
                       gradient_checkpointing=True, bf16=True, max_length=args.max_length,
                       lr_scheduler_type="cosine", warmup_steps=0.05, logging_steps=5,
                       save_strategy="epoch", seed=args.seed, report_to=[])
    trainer = SFTTrainer(model=model, args=config, train_dataset=Dataset.from_list(kept),
                         processing_class=tok, peft_config=lora)
    trainer.train()
    trainer.save_model(str(adapter))
    del trainer, model
    torch.cuda.empty_cache()

    # Merge into the full-precision base (merging into 4-bit weights loses accuracy).
    base = AutoModelForCausalLM.from_pretrained(args.base, dtype=torch.bfloat16)
    merged = PeftModel.from_pretrained(base, str(adapter)).merge_and_unload()
    out = args.data / "model"
    merged.save_pretrained(str(out), safe_serialization=True)
    tok.save_pretrained(str(out))
    # Ollama 0.13+ cannot import Qwen2 safetensors directly; it loads a GGUF converted
    # with llama.cpp's convert_hf_to_gguf.py (docs/LOCAL_MODEL.md, step 4).
    (out / "Modelfile").write_text("FROM ./igs-call.gguf\nPARAMETER temperature 0\n"
                                   f"PARAMETER num_ctx {args.max_length}\n", encoding="utf-8")
    print(f"Merged model in {out}. Convert it to {out / 'igs-call.gguf'}, then: "
          f"ollama create igs-call -f {out / 'Modelfile'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
