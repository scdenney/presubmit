# presubmit

**Claude-powered adversarial peer review for academic PDFs: red-team critique, claim verification, and structured review reports.**

A derivative of [`reviewer2`](https://github.com/isitcredible/reviewer2) (Apache-2.0, © The Catalogue of Errors Ltd) with the LLM client layer rewritten to call Claude instead of Gemini. All prompts, pipeline structure, and non-LLM tooling (PDF merge, code ingestion, Mathpix math OCR) are ported largely verbatim from upstream; only the API client and model routing are new.

> **If you are deciding between this and upstream:** try upstream `reviewer2` on Gemini first if you can. It is the reference implementation, has been benchmarked against 5 alternatives (15 wins / 4 ties / 1 loss in the accompanying paper), and uses Gemini features (grounded search, permissive safety overrides) that this port cannot fully replicate. Use `presubmit` when you need Claude (budget, stack, policy preference) or want to run the pipeline against a different model family for comparison. See "Known trade-offs" below.

## What it does

Takes a manuscript PDF and produces a plain-text critical review via a 30+ stage chain of LLM calls. The chain runs an adversarial structure:

- **Red Team** agents (Breaker, Butcher, Shredder, Collector, Void) read the paper aggressively and list every flaw they can find, with extensive quotes.
- **Blue Team** defends: goes through each red-team issue and produces an honest defence where one exists.
- **Verification cascade**: numbers audit, assessment agent, list compilation; then a fact-checker walks citations and external claims, and an external-check agent validates quotes.
- **Review assembly + legal pass**: reviewer agent synthesises; a second cross-check + reviser cleans; a legal pass softens any claim-like language.
- **Writer Mode** (default on): alchemist + polisher + proofreader + copyeditor produce a polished author-facing version of the review.

Output is one `report.txt` plus, optionally, an editor's note and copyediting suggestions. Runs are **resumable**: deleting a stage file in the work directory forces that stage (and dependents) to re-run on the next invocation.

## PDF handling and cost

`presubmit` does **not** re-upload the source PDF to every Claude call. It would be a token muncher: an 80-page paper sent as PDF burns ~500K input tokens per stage (page rasters are ~1.6K tokens each), and across 30+ stages that runs ~$25–60 per paper on Opus 4.7. So the pipeline does not work that way.

Instead, the pipeline converts the PDF to markdown **once** at start (using [`marker-pdf`](https://github.com/VikParuchuri/marker), which preserves table structure and figure captions) and routes ~25 of the ~30 stages to that markdown. The markdown is sent as a cache-controlled content block, so the second through Nth stages within Anthropic's 5-minute prompt-cache window pay roughly 10% of the first call's input cost.

The 7 stages that genuinely need page rasters — the math chain (`01e`, `01e2`, `01fa`–`01fd`) and `09a_proofreader` (which checks layout) — keep using the PDF directly. Everything else reasons over the markdown.

**Estimated cost on a typical 80-page paper, Opus 4.7:** ~$2–4 per full run with this routing. Without the PDF→markdown step, the same paper would cost ~$25–30.

**Why `marker-pdf` is a hard dependency.** `marker-pdf` is what makes the converted markdown high-fidelity enough to substitute for the PDF on text-only stages. Plain `pypdf` text extraction loses table structure and figure captions, which materially weakens what the Butcher / Numbers / Reviewer stages can reason about on table-heavy papers. The pipeline therefore refuses to fall back to it; if marker fails to import or convert, the run halts with `PipelineError` rather than producing a degraded review silently. If you need to bypass marker (e.g., you already have the source as `.md` or `.tex`), pass that file directly — the CLI accepts any of `.pdf`, `.md`, `.markdown`, `.txt`, `.tex`.

**Cost of the dependency itself.** `marker-pdf` pulls in PyTorch and a few GB of ML model weights (downloaded on first conversion to the local Hugging Face cache, reused after that). Initial install is slow (~5–10 minutes); first conversion downloads ~3–5 GB of models; subsequent conversions take ~30 seconds to a few minutes per PDF depending on page count and whether you have GPU/MPS available.

## Requirements

- Python 3.10+
- An Anthropic API key ([console.anthropic.com](https://console.anthropic.com/))
- `marker-pdf` (installed automatically via `pip install -e .`; pulls in PyTorch and a few GB of ML models on first use — see "PDF handling and cost" above)
- `qpdf` on `PATH` for PDF preprocessing (optional; python fallback exists)
- (Optional) A Mathpix account for the math-audit add-on

## Install

Clone and install in editable mode:

```bash
git clone https://github.com/scdenney/presubmit
cd presubmit
python3 -m venv .venv && source .venv/bin/activate    # recommended; marker pulls in heavy deps
pip install -e .
```

Initial install takes 5–10 minutes because of `marker-pdf` and its PyTorch dependency. The first PDF conversion downloads model weights (~3–5 GB) the first time.

## API key setup

`presubmit` calls the Anthropic API directly via the official Python SDK. It does **not** authenticate via the `claude` CLI's OAuth subscription or via your claude.ai login — those are different auth surfaces. You need a personal API key on your Anthropic account.

1. Generate a key at [console.anthropic.com](https://console.anthropic.com/) → **Settings** → **API Keys** → **Create Key**. Keys look like `sk-ant-api03-...`.
2. Add an export line to your shell rc (e.g. `~/.zshrc` for zsh, `~/.bashrc` for bash):

   ```bash
   export ANTHROPIC_API_KEY="sk-ant-api03-..."
   ```

   If your shell rc also has wrapper functions or aliases that set `ANTHROPIC_API_KEY` to a different value (e.g. setting it to `""` to route the `claude` CLI through a local Ollama server), put the real `export` line **above** those wrappers so a later assignment doesn't shadow the key in your default shell environment.

3. Reload the shell (`source ~/.zshrc` or open a new terminal) and verify:

   ```bash
   echo "${ANTHROPIC_API_KEY:0:8}…"   # should print sk-ant-a…
   ```

4. Make sure the account has a positive credit balance. The pipeline hits the API 30–40 times per paper; without credit it halts on the first call (the fail-fast behavior is intentional — see commit history).

The key is billed to your Anthropic account and is independent of any Claude Code or claude.ai subscription you have.

## Output location

By default, intermediate stage outputs land in a temp directory that gets cleaned up after the run — almost never what you want, since the per-stage files (`01a_breaker.txt`, `02e_assessment.txt`, etc.) are often more useful than the consolidated final report. Three ways to control where outputs land, in order of precedence:

1. **`--work-dir <path>` flag** (highest priority). Always wins, never auto-cleaned. Use this for one-off runs or when you want to override the default.
2. **`PRESUBMIT_OUTPUT_BASE` env var** (recommended). If set, presubmit derives `<base>/<slug>/presubmit_run/` from the input filename automatically. Set once in your shell rc and forget about `--work-dir`:

   ```bash
   export PRESUBMIT_OUTPUT_BASE="$HOME/presubmit-reviews"   # or wherever you want
   ```

   The slug is the input filename, lowercased, with non-alphanumeric runs collapsed to single hyphens. So `Denney_2026_What-Were-They-Thinking.pdf` becomes `denney_2026_what-were-they-thinking`, and the run lands in `~/presubmit-reviews/denney_2026_what-were-they-thinking/presubmit_run/`.

3. **Neither set** — falls back to a temp dir with a warning telling you to set one of the above. The pipeline still runs, but the stage files may be garbage-collected.

## Quickstart

```bash
presubmit paper.pdf
```

The CLI accepts `.pdf`, `.md`, `.markdown`, `.txt`, and `.tex` (the last is auto-converted via `pandoc`). For PDFs the conversion-to-markdown step runs once at the start of the pipeline and is cached in the work directory.

A default run hits the API 30–40 times. Wall time is ~15–45 minutes depending on paper length and how much Extended Thinking the heavy-reasoning stages use. Cost depends on which Claude tier each stage routes to (see `src/presubmit/core.py` → `MODELS`).

For a cheap smoke run, force every stage to Haiku:

```bash
CLAUDE_MODEL_OVERRIDE=haiku presubmit paper.pdf -o smoke.txt
```

To stop after the Red Team passes (useful for verifying the markdown conversion + first round of stages without committing to a full run):

```bash
presubmit paper.pdf -o smoke.txt --stop-stage 2.0
```

## Known trade-offs vs. upstream Gemini

This port is not a perfect replica. Four places the Gemini and Claude implementations diverge:

### 1. Safety policy

Upstream disables four Gemini harm categories (`HARM_CATEGORY_HATE_SPEECH`, `DANGEROUS_CONTENT`, `SEXUALLY_EXPLICIT`, `HARASSMENT`) with `BLOCK_NONE` thresholds so the Red Team can use blunt, adversarial language without being filtered. **Claude has no equivalent override.** Prompts that read as *ad hominem attack on the author* or *fraud accusation* may be refused by Claude even though the *task* (critical academic peer review) is clearly legitimate.

**What this means in practice:** Red Team prompts ported verbatim occasionally trigger `stop_reason == "refusal"`. The Python client raises a `FATAL: Claude refused` error when that happens. Two mitigations:

- **Prompt softening.** Rephrase adversarial language to target the *manuscript's claims* rather than the *authors' character* ("the argument breaks on X" rather than "this is fraudulent"). The substantive pressure on the paper is preserved; only the rhetoric changes.
- **Retry with a heavier model.** Opus is more willing to engage with pointed critique than Haiku.

If you hit systematic refusals on a specific stage, file an issue with the stage ID and the prompt text.

### 2. Grounded web search

Upstream stage `00a_metadata` uses Gemini's `GoogleSearch` tool to look up the paper on the open web when metadata is ambiguous (for example, to resolve a title to a DOI, find the published citation, verify an author affiliation). **Claude has no native grounded-search tool.** The port accepts the `use_search=True` kwarg but currently *ignores it* and logs a warning.

**What this costs:** metadata extraction on unpublished manuscripts is unaffected (the PDF itself is the source). For published papers where the PDF lacks a clean citation block, the metadata fields (DOI, canonical venue, author affiliation) degrade: Claude can only use what's visible in the PDF. Downstream stages are unaffected.

**Workarounds if you need it:**
- Add a Tavily, Brave, or SerpAPI call inside `call_claude(..., use_search=True)` — the hook is there; the implementation is a TODO.
- Run inside Claude Code (where Claude has a `WebSearch` tool) and wire that through.
- Accept degraded metadata and hand-edit the citation field before writing the report.

### 3. Extended thinking semantics

Gemini's `ThinkingConfig` takes either a `thinking_budget` (integer token count, or `-1` for "unbounded") or a `thinking_level` ("low" / "medium" / "high"). Claude's extended thinking takes `budget_tokens` only.

The port translates:
- `thinking_budget=N` → `{"type": "enabled", "budget_tokens": N}` if `N >= 1024`, else disabled
- `thinking_budget=-1` → `budget_tokens=12000` (approximate "unbounded")
- `thinking_level="low" / "medium" / "high"` → `budget_tokens=2000 / 5000 / 10000`

The `high` setting is deliberately conservative — Claude's thinking tokens are priced per-token output, so ramping higher than 10k on 30+ stages adds up fast. If you find specific stages under-thinking, bump the `THINKING_LEVEL_TO_BUDGET` map in `core.py`.

Also: Claude's extended thinking **requires `temperature=1`**. The port overrides any caller-supplied temperature when thinking is enabled. Stages that rely on `temperature=0.0` for determinism will get `temperature=1.0` silently whenever they also use thinking. In practice this has minimal impact on output consistency for the review task.

### 4. Model tier mapping

Upstream assigns stages to specific Gemini model keys (`flash_lite`, `flash_2_5`, `pro_2_5`, `pro_3`, `pro_3_1`). The port maps these to Claude as follows:

| Upstream key   | Claude model         | Use case                              |
|----------------|----------------------|---------------------------------------|
| `flash_lite`   | `claude-haiku-4-5`   | Light validators, structure checks    |
| `flash_2_5`    | `claude-sonnet-4-6`  | Mid-tier reasoning (Red Team support) |
| `pro_2_5`      | `claude-sonnet-4-6`  | Red Team primary                      |
| `pro_3_1`      | `claude-opus-4-7`    | Heavy reasoning, review synthesis     |

This mapping is a starting point, not a calibrated equivalence. Claude Sonnet is plausibly a closer stand-in for Gemini Pro 2.5 than for Flash; Claude Opus is plausibly overkill for some Gemini Pro 3.1 stages. Treat it as a tunable dial in `src/presubmit/core.py`.

## Cost tracking

Upstream ships a `pricing.csv` keyed by Gemini model names. **This port has not updated that file for Claude pricing.** The `calculate_cost()` helper will therefore report `MISSING` for every stage until you populate `src/presubmit/data/pricing.csv` with Claude per-million-token rates. This is a known TODO — costs are still tracked by the API dashboard; only the end-of-run report is affected.

## What's the same as upstream

- All 46 stage prompts (except the persona name change "Reviewer 2" → "Critical Reviewer" per the upstream trademark NOTICE).
- Pipeline sequencing, checkpoint resumability, work-dir layout.
- PDF preprocessing (qpdf + pypdf fallback), supplement merging, code-zip ingestion.
- Mathpix math-OCR integration (opt-in).
- Output formats: plain-text report, optional editor's note, optional copyediting suggestions.
- Report rendering (`render_text.py`).

## Related peer-review automation systems

For context on where this sits in the landscape of LLM-based review tooling:

- **[reviewer2](https://github.com/isitcredible/reviewer2)** (this project's upstream) — Gemini, adversarial + verification. The reference implementation.
- **[MARG](https://arxiv.org/abs/2401.04259)** (D'Arcy et al. 2024) — Multi-Agent Review Generation. Splits a paper into sections, assigns specialist agents (clarity, experiments, related work), produces aggregated feedback.
- **Liang et al. 2023, "Can Large Language Models Provide Useful Feedback on Research Papers?"** (Stanford) — single-shot GPT-4 review; benchmarked against human reviewers on Nature Portfolio / ICLR papers.
- **Yuan, Liu & Neubig 2022, "Can We Automate Scientific Reviewing?"** (CMU) — earlier encoder-decoder approach; useful baseline for what pre-LLM automation looked like.
- **OpenReview automation** — various ML-conference prototypes for reviewer-paper matching, novelty flagging, and reference completeness checks. Not a drop-in pipeline.
- **[AgentReview](https://arxiv.org/abs/2406.12708)** (Jin et al. 2024) — simulates reviewer-AC-author loops rather than producing a single review.

These differ from `presubmit` in two axes: (a) whether they run an **adversarial+verification** cascade (reviewer2 and its fork do; most others produce a single integrated review) and (b) whether they are a **practical CLI/service** versus a **research prototype**. Our port inherits upstream's adversarial architecture and CLI mode. The Liang et al. paper and MARG are worth reading if you want to understand why pure "ask the LLM once" approaches tend to miss the subtle methodological issues reviewer2 catches.

## License

CC BY 4.0. See [LICENSE](LICENSE).

This project is built for remixing, reuse, and adaptation, including commercial use, with attribution. It is derived from [`reviewer2`](https://github.com/isitcredible/reviewer2), so upstream Apache-2.0 attribution and trademark notes are preserved in [NOTICE](NOTICE), with the upstream license text retained at [LICENSES/Apache-2.0.txt](LICENSES/Apache-2.0.txt).

## Status

**Beta.** The port compiles and the API surface is covered, but:
- No end-to-end smoke-tested run has been recorded yet.
- Pricing CSV is stale (Claude rates not populated).
- The `use_search` replacement is a stub.
- Red Team prompts have not been systematically softened for Claude's safety policy — expect some refusals on first runs; file issues.

Contributions welcome. If you run a full review and can share a timing + refusal-incidence report, that's especially useful.
