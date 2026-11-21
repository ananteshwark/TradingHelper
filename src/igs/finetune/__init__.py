"""Fine-tune a local model (Qwen 2.5 7B by default) on the app's own AI stock calls.

dataset: read matured calls from the server database and write chat-format JSONL, split by
         the date the call was made (train before the cutoff, evaluation after it, with an
         embargo so no training outcome overlaps an evaluation call).
train:   QLoRA on a local NVIDIA GPU, then merge and write an Ollama Modelfile.
evaluate: ask the local model through Ollama for each evaluation call and score its actions
         against the realised excess return, next to the cloud model's own calls.

A trained model is a research result, not a deployed provider: nothing here routes app
tasks to it.
"""
