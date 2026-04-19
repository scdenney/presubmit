# CLAUDE.md — Project Guide

## What this is

`presubmit` is an adversarial peer-review pipeline for academic PDFs, ported from the upstream `reviewer2` (isitcredible.com, Apache-2.0) to run on Anthropic Claude instead of Google Gemini.

## Key facts for future maintenance

- **Upstream remains the reference implementation.** Prompts and pipeline structure are copied largely verbatim; only the LLM client layer (`core.py`) and model routing differ. When upstream releases changes to prompts, merge them in. When they change the pipeline structure, consider porting.
- **LICENSE + NOTICE are load-bearing.** Apache-2.0 requires preserving upstream attribution. Upstream's NOTICE blocks use of their trademarks ("Reviewer 2", "isitcredible"); we comply by using the name `presubmit` and renaming the persona "Reviewer 2" → "Critical Reviewer" in all prompt files. Do NOT restore the upstream names.
- **`call_gemini` is an alias for `call_claude`.** This exists so the 40+ stage functions in `stages.py` don't need to change. Don't rename either function.
- **Models are aliased in `core.py::MODELS`.** Upstream stage code references `flash_lite`, `pro_2_5`, etc.; the map translates those to `claude-haiku-4-5` / `claude-sonnet-4-6` / `claude-opus-4-7`. If upstream adds new model keys, add them there.
- **Red Team prompts may hit safety refusals.** Claude has no `BLOCK_NONE` override like Gemini. When porting new upstream prompts or writing new ones, prefer attacking the *manuscript's claims* over *the authors' character*. If `stop_reason=="refusal"` keeps firing, soften rhetoric rather than escalating.
- **Grounded search is a no-op.** The `use_search=True` kwarg is accepted but ignored with a warning. If this becomes important, wire Tavily or SerpAPI into `call_claude` or plumb a Claude Code `WebSearch` tool through.
- **Pricing CSV is stale.** `src/presubmit/data/pricing.csv` still has Gemini rates. `calculate_cost()` will emit `MISSING` for Claude models until this is updated.

## Architecture recap

Directory layout:

```
src/presubmit/
├── __init__.py         # public API
├── cli.py              # argparse entry; calls pipeline.run()
├── core.py             # Claude client (call_claude, file upload, retries)
├── pipeline.py         # stage orchestrator, resumability, work-dir mgmt
├── stages.py           # 30+ stage functions, each calls core.call_claude
├── helpers.py          # code ingestion, metadata parsing, cost calc
├── render_text.py      # plain-text report assembly
├── mathpix.py          # optional Mathpix math-OCR client
├── paths.py            # prompts/pricing path resolution + env overrides
├── prompts/            # 46 stage prompt .txt files + resources/
└── data/pricing.csv    # per-model pricing (STALE — Gemini rates)
```

## Environment variables

- `ANTHROPIC_API_KEY` — required.
- `CLAUDE_MODEL_OVERRIDE` — force every stage to a single model key (for smoke runs).
- `PRESUBMIT_PROMPTS_DIR` — override the packaged prompts directory.
- `PRESUBMIT_PRICING_CSV` — override the packaged pricing CSV.
- `MATHPIX_APP_ID`, `MATHPIX_APP_KEY` — optional, for math-audit add-on.

## When adding a new stage

1. Write the prompt as `src/presubmit/prompts/NN_stage_name.txt`.
2. Add a stage function in `stages.py` that calls `call_gemini(...)` with the right `model_type` and `thinking_budget` / `thinking_level`. (Keep the call_gemini alias — it maps to call_claude internally.)
3. Wire the stage into `pipeline.py`'s sequence.
4. Consider whether the stage needs red-team-style adversarial language. If so, soften preemptively to avoid Claude refusals.
