# CLAUDE.md — Project Guide

## Next up (calibration pass)

Nothing blocking — the port compiles and all cross-references resolve. When returning to this repo, pick up in roughly this order:

1. **Smoke-test on one real PDF.** `export ANTHROPIC_API_KEY=...`, `pip install -e .`, then `presubmit some-paper.pdf -o report.txt`. Watch for:
   - `FATAL: Claude refused` — a Red Team prompt tripped Claude safety. Note which stage (the error message includes it); softening will be needed in `src/presubmit/prompts/01*.txt` or `02*.txt` most likely. The task is critical peer review, not personal attack; rewording usually clears refusals.
   - Wall time and whether thinking budgets feel proportionate. Upstream's Gemini `thinking_budget=-1` mapped to `budget_tokens=12000` on Claude; high-reasoning stages (01a Breaker, 02e Assessment, 04a Reviewer) may want more.
   - Model tier mapping quality. `MODELS` in `core.py` aliases Gemini keys → Claude; if Sonnet feels weak for Red Team stages, promote to Opus.
2. ~~**Populate `src/presubmit/data/pricing.csv`** with Claude per-M-token rates.~~ Done — current Haiku 4.5 / Sonnet 4.6 / Opus 4.8 rates are in the CSV and `calculate_cost()` resolves them.
3. ~~**Decide the `use_search` path.**~~ Done in 0.2.0 — `use_search=True` attaches Anthropic's server-side web search tool.
4. ~~**Add a minimal smoke test.**~~ Done in 0.2.0 — `tests/` holds upstream's offline end-to-end run plus client-layer tests (`pytest -q tests`).

Things that do **not** need work: commits, push, cross-refs, sync with the sibling `paper-review-lite` skill in open-science-skills. All of that is already live.

## What this is

`presubmit` is an adversarial peer-review pipeline for academic PDFs, ported from the upstream `reviewer2` (isitcredible.com, Apache-2.0) to run on Anthropic Claude instead of Google Gemini.

## Key facts for future maintenance

- **Upstream is finished.** reviewer2 v2.0.0 (2026-10-26) is its final release, and 0.2.0 ports it. Prompts and pipeline structure are copied largely verbatim; the LLM client layer (`core.py`), model tiers, the markdown route for text stages, the per-stage disk cache in `pipeline._run_stage_with_retry`, and four prompt additions from `research_skills` (README, "Changes to upstream prompts") are ours. With no upstream releases left to merge, prompt edits here no longer cost future merges.
- **LICENSE + NOTICE are load-bearing.** The repo is distributed under CC BY 4.0, but Apache-2.0 still requires preserving upstream attribution for `reviewer2`-derived material. Keep `NOTICE` and `LICENSES/Apache-2.0.txt` in sync. Upstream's NOTICE blocks use of their trademarks ("Reviewer 2", "isitcredible"); we comply by using the name `presubmit` and renaming the persona "Reviewer 2" → "Critical Reviewer" in all prompt files. Do NOT restore the upstream names.
- **`call_gemini` is an alias for `call_claude`.** This exists so the 40+ stage functions in `stages.py` don't need to change. Don't rename either function.
- **Models are tiered in `core.py::MODELS`** (`mechanical`, `forensic`, `adversarial`, `synthesis`). Every `model_type=` in `stages.py`, `ref_checker.py`, and the page-map call in `pipeline.py` names a tier, not an upstream Gemini key.
- **Thinking is routed per model class in `core.py`.** `_ADAPTIVE_THINKING_MODELS` use `thinking={"type":"adaptive"}` + `output_config={"effort": ...}`; everything else (Haiku 4.5 and older) uses legacy `{"type":"enabled","budget_tokens":N}`. When adding a new model alias, slot it into that list or its calls fall back to the budget path. `temperature` is never sent: API version 2026-10-21 rejects it on every model, and the anthropic 2.x SDK raises `TypeError` on it.
- **Red Team prompts may hit safety refusals.** Claude has no `BLOCK_NONE` override like Gemini. When porting new upstream prompts or writing new ones, prefer attacking the *manuscript's claims* over *the authors' character*. If `stop_reason=="refusal"` keeps firing, soften rhetoric rather than escalating.
- **Grounded search uses Anthropic's server-side web search** (`_web_search_tool` in `core.py`: the dynamic variant on Sonnet/Opus, basic on Haiku). `CODE_EXEC_STEPS` attaches server-side code execution. Paused server-tool turns (`stop_reason == "pause_turn"`) are resent up to five times. Search fees are not in `calculate_cost()`.
- **Pricing CSV is populated.** `src/presubmit/data/pricing.csv` carries current Claude per-MTok rates (Haiku 4.5 $1/$5, Sonnet 4.6 $3/$15, Opus 4.8 $5/$25); `calculate_cost()` resolves them. Refresh if Anthropic changes pricing or the `MODELS` map adds a new model.

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
└── data/pricing.csv    # per-model pricing (current Claude per-MTok rates)
```

## Environment variables

- `ANTHROPIC_API_KEY` — required.
- `PRESUBMIT_OUTPUT_BASE` — default output base; without `--work-dir`, runs land in `<base>/<slug>/presubmit_run/` (the recommended setup per the README).
- `CLAUDE_MODEL_OVERRIDE` — force every stage to a single model key (for smoke runs).
- `PRESUBMIT_PROMPTS_DIR` — override the packaged prompts directory.
- `PRESUBMIT_PRICING_CSV` — override the packaged pricing CSV.
- `MATHPIX_APP_ID`, `MATHPIX_APP_KEY` — optional, for math-audit add-on.

## When adding a new stage

1. Write the prompt as `src/presubmit/prompts/NN_stage_name.txt`.
2. Add a stage function in `stages.py` that calls `call_gemini(...)` with the right `model_type` and `thinking_budget` / `thinking_level`. (Keep the call_gemini alias — it maps to call_claude internally.)
3. Wire the stage into `pipeline.py`'s sequence.
4. Consider whether the stage needs red-team-style adversarial language. If so, soften preemptively to avoid Claude refusals.
