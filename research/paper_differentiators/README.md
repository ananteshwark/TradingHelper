# Paper-call differentiators

[The research reference](../../docs/PAPER_CALL_DIFFERENTIATORS.md) compares stock-level inputs,
previous-session activity and market context for profitable and losing paper calls, by model
and trade side. It includes temporal and within-day checks and an annotation checklist for
future calls. This is exploratory research, not a deployed trading rule.

`analyze.py` uses Python's standard library and accepts the frozen numerical snapshot from
the server audit. It emits all numeric comparisons, not only the findings selected for the
write-up. Data stays in the ignored reports directory; credentials are never inputs.

`hypotheses.json` is a research reference with evidence, units, caveats and source hashes.
It contains no execution thresholds and is not read by any production model or order path.

The earlier report tested single-feature median filters. This follow-up describes side-specific
relationships after inspecting that same sample; neither the earlier nor later block is now an
independent test set. Any new rule requires a fresh forward evaluation.
