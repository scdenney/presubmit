"""Claude API client with upload caching, retry logic, and PDF tools.

Ported from upstream `reviewer2` (Apache-2.0, isitcredible/reviewer2); the
public surface (`call_gemini`, `get_or_upload_file`, `cleanup_resources`,
`save_output`, `load_prompt`, `sanitize_pdf_ghostscript`, `merge_pdfs_python`)
is preserved so the existing stage code works unchanged. Internally the LLM
client is Anthropic Claude; `call_gemini` is aliased to `call_claude`.
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

import anthropic
from pypdf import PdfReader, PdfWriter

from presubmit.paths import prompts_dir

# Per-call usage records. Appended by call_claude, consumed by helpers.calculate_cost.
USAGE_LOG: list[dict] = []

# { local_file_path: claude_file_id }. Reset with cleanup_resources.
_FILE_CACHE: dict[str, str] = {}
# Guards _FILE_CACHE so parallel stages don't race on the upload path.
_FILE_CACHE_LOCK = threading.Lock()

# Upstream reviewer2 model keys → Claude equivalents.
# Haiku for light/fast stages, Sonnet for mid-tier, Opus for heavy reasoning.
MODELS = {
    # Claude-native aliases
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
    # Upstream Gemini keys remapped so stages.py works unchanged
    "flash_lite": "claude-haiku-4-5",
    "flash_lite_3": "claude-haiku-4-5",
    "flash_2_5": "claude-sonnet-4-6",
    "flash_3": "claude-sonnet-4-6",
    "pro_2_5": "claude-sonnet-4-6",
    "pro_3": "claude-opus-4-7",
    "pro_3_1": "claude-opus-4-7",
}

# Anthropic API requires max_tokens. Default if caller does not pass one.
DEFAULT_MAX_TOKENS = 16000

# Gemini `thinking_level` → Claude extended-thinking budget_tokens.
# Used only on models that still accept the legacy `{"type": "enabled",
# "budget_tokens": N}` thinking config (Sonnet 4.6, Haiku 4.5, etc.).
THINKING_LEVEL_TO_BUDGET = {"low": 2000, "medium": 5000, "high": 10000}

# Models that require the new adaptive-thinking API
# (`thinking.type=adaptive` + `output_config.effort=low|medium|high`)
# instead of the legacy `{"type": "enabled", "budget_tokens": N}` format.
# Opus 4.7 deprecated the legacy form.
_ADAPTIVE_THINKING_MODELS = ("opus-4-7",)


def _budget_to_effort(budget: int) -> str:
    """Bucket a token budget into the adaptive-thinking effort tier."""
    if budget < 3000:
        return "low"
    if budget < 8000:
        return "medium"
    return "high"

# Beta flag required for the Files API.
_FILES_API_BETA = "files-api-2025-04-14"


def get_or_upload_file(client: anthropic.Anthropic, file_path: str, force_upload: bool = False) -> str:
    """Upload a PDF to Claude once per session; reuse the file_id after that.

    Thread-safe: parallel stages calling this with the same ``file_path`` will
    serialise through ``_FILE_CACHE_LOCK`` so only one upload happens.
    """
    global _FILE_CACHE

    with _FILE_CACHE_LOCK:
        if not force_upload and file_path in _FILE_CACHE:
            return _FILE_CACHE[file_path]

        print(f"    ↳ 📤 Uploading NEW file to Claude: {os.path.basename(file_path)}...")
        try:
            with open(file_path, "rb") as fh:
                uploaded = client.beta.files.upload(
                    file=(os.path.basename(file_path), fh, "application/pdf"),
                    betas=[_FILES_API_BETA],
                )
            file_id = uploaded.id
            _FILE_CACHE[file_path] = file_id
            print(f"    ✓ Upload complete: {file_id} (Cached)")
            return file_id
        except Exception as e:
            if file_path in _FILE_CACHE:
                del _FILE_CACHE[file_path]
            raise IOError(f"Error uploading PDF '{file_path}': {e}")


def cleanup_resources():
    """Delete every remote file uploaded during this session."""
    global _FILE_CACHE
    if not _FILE_CACHE:
        return

    print("\n🧹 Cleaning up Claude resources...")
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return

    try:
        client = anthropic.Anthropic(api_key=api_key)
        for _path, file_id in list(_FILE_CACHE.items()):
            try:
                client.beta.files.delete(file_id=file_id, betas=[_FILES_API_BETA])
                print(f"  ✓ Deleted remote file: {file_id}")
            except Exception as e:
                if "404" not in str(e):
                    print(f"  ⚠ Failed to delete {file_id}: {e}")
        _FILE_CACHE.clear()
    except Exception as e:
        print(f"  ⚠ Cleanup initialization failed: {e}")


atexit.register(cleanup_resources)


def call_claude(
    prompt=None,
    pdf_file_path=None,
    model_type="flash_lite",
    temperature=0.1,
    thinking_level=None,
    thinking_budget=None,
    media_resolution=None,  # kept for upstream signature compat; unused by Claude
    system_instruction=None,
    max_retries=10,
    retry_forever_on_rate_limit=True,
    step=None,
    use_search=False,
    max_output_tokens=None,
):
    """Call Claude with retries, PDF caching, and extended-thinking support.

    ``CLAUDE_MODEL_OVERRIDE`` in the environment forces every call to a single
    model, useful for cheap smoke runs against Haiku.

    Note: ``use_search`` is accepted but currently a no-op — Claude has no
    native grounded search. See README "Known trade-offs vs. upstream".
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not found in environment")

    override = os.environ.get("CLAUDE_MODEL_OVERRIDE")
    if override:
        model_type = override

    model_name = MODELS.get(model_type, model_type)
    if not model_name:
        print(f"  ⚠  Warning: Unknown model_type '{model_type}', falling back to haiku")
        model_name = MODELS["haiku"]

    client = anthropic.Anthropic(api_key=api_key)

    # Extended thinking configuration.
    # Two API shapes coexist:
    #   - legacy:   thinking={"type": "enabled", "budget_tokens": N}
    #   - adaptive: thinking={"type": "adaptive"}, output_config={"effort": "low|medium|high"}
    # Opus 4.7 dropped legacy. We pick per-model.
    use_adaptive = any(tag in model_name for tag in _ADAPTIVE_THINKING_MODELS)
    thinking_cfg = None
    output_config = None

    # Gemini's `thinking_budget=-1` means "unbounded" — we approximate with high budget.
    if thinking_budget is not None:
        budget = 12000 if int(thinking_budget) < 0 else int(thinking_budget)
        if budget >= 1024:  # Anthropic minimum
            if use_adaptive:
                thinking_cfg = {"type": "adaptive"}
                output_config = {"effort": _budget_to_effort(budget)}
            else:
                thinking_cfg = {"type": "enabled", "budget_tokens": budget}
    elif thinking_level:
        if use_adaptive:
            effort = thinking_level if thinking_level in ("low", "medium", "high") else "medium"
            thinking_cfg = {"type": "adaptive"}
            output_config = {"effort": effort}
        else:
            budget = THINKING_LEVEL_TO_BUDGET.get(thinking_level, 5000)
            thinking_cfg = {"type": "enabled", "budget_tokens": budget}

    # Resolve max_tokens. Required by Anthropic API; must exceed legacy thinking budget.
    max_tok = max_output_tokens or DEFAULT_MAX_TOKENS
    if thinking_cfg and thinking_cfg.get("type") == "enabled":
        if max_tok <= thinking_cfg["budget_tokens"]:
            max_tok = thinking_cfg["budget_tokens"] + 8000

    if use_search:
        print(
            "    ⚠  use_search=True requested, but Claude has no native grounded "
            "search. Proceeding without web-grounded results. "
            "See README for workarounds."
        )

    attempt = 0
    base_delay = 5
    max_delay = 300
    has_forced_reupload = False

    while True:
        attempt += 1

        # Build the user-message content.
        content_parts = []
        if pdf_file_path:
            force = attempt > 1 and has_forced_reupload
            try:
                file_id = get_or_upload_file(client, pdf_file_path, force_upload=force)
                has_forced_reupload = False
                content_parts.append(
                    {"type": "document", "source": {"type": "file", "file_id": file_id}}
                )
            except Exception as e:
                print(f"    ⚠  Upload failed inside retry loop: {e}")
                time.sleep(5)
                continue

        if prompt:
            content_parts.append({"type": "text", "text": prompt})

        messages = [{"role": "user", "content": content_parts}]

        kwargs = {
            "model": model_name,
            "max_tokens": max_tok,
            "temperature": temperature,
            "messages": messages,
        }
        if system_instruction:
            kwargs["system"] = system_instruction
        if thinking_cfg:
            kwargs["thinking"] = thinking_cfg
            if output_config:
                kwargs["output_config"] = output_config
            # Both legacy and adaptive thinking on Claude require temperature=1.
            kwargs["temperature"] = 1.0

        try:
            if pdf_file_path:
                response = client.beta.messages.create(betas=[_FILES_API_BETA], **kwargs)
            else:
                response = client.messages.create(**kwargs)

            # Usage log.
            usage = getattr(response, "usage", None)
            if usage:
                p_tok = getattr(usage, "input_tokens", 0) or 0
                c_tok = getattr(usage, "output_tokens", 0) or 0
                # Claude bundles thinking tokens inside output_tokens; we don't
                # have a separate breakdown. Keep the field for CSV compat.
                USAGE_LOG.append(
                    {
                        "model_name": model_name,
                        "input_tokens": p_tok,
                        "output_tokens": c_tok,
                        "thoughts_tokens": 0,
                        "timestamp": datetime.now().isoformat(),
                        "step": step or "unknown",
                    }
                )

            stop_reason = getattr(response, "stop_reason", None)

            # Refusal — treat as FATAL (safety-policy mismatch vs. Gemini's BLOCK_NONE).
            if stop_reason == "refusal":
                error_msg = (
                    f"FATAL: Claude refused the request (likely safety policy). "
                    f"stop_reason={stop_reason}. "
                    "See README 'Known trade-offs' — may require prompt softening."
                )
                print(f"    ❌ {error_msg}")
                raise RuntimeError(error_msg)

            # Extract text blocks (skip thinking/tool_use blocks).
            final_text = []
            for block in response.content:
                block_type = getattr(block, "type", None)
                if block_type == "text":
                    final_text.append(block.text)

            result = "".join(final_text).strip()

            if not result:
                if stop_reason == "max_tokens":
                    raise ValueError(
                        f"FATAL: Model hit max_tokens and returned no content. "
                        f"(stop_reason={stop_reason})"
                    )
                raise ValueError(f"API returned empty text (stop_reason={stop_reason})")
            return result

        except anthropic.APIStatusError as e:
            err_str = str(e)
            status_code = getattr(e, "status_code", None)

            if "FATAL" in err_str:
                raise

            if status_code == 403 and ("file" in err_str.lower() or "not_found" in err_str.lower()):
                print("    ⚠  File Permission/Access Error (403). Forcing re-upload next attempt.")
                has_forced_reupload = True
                if pdf_file_path and pdf_file_path in _FILE_CACHE:
                    del _FILE_CACHE[pdf_file_path]
                time.sleep(2)
                if attempt >= max_retries:
                    raise RuntimeError(f"File access failed after {attempt} attempts: {err_str}")
                continue

            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)

            if status_code == 429:
                print(f"    ↳ Rate Limited (429) on attempt {attempt}. Waiting {delay}s...")
                time.sleep(delay)
                if not retry_forever_on_rate_limit and attempt >= max_retries:
                    raise RuntimeError(f"Rate limit exceeded after {attempt} attempts: {err_str}")
                continue
            elif status_code in (500, 502, 503):
                print(f"    ⚠  Server Error ({status_code}, attempt {attempt}). Waiting {delay}s...")
                time.sleep(delay)
                if not retry_forever_on_rate_limit and attempt >= max_retries:
                    raise RuntimeError(f"Server error after {attempt} attempts: {err_str}")
                continue
            else:
                print(
                    f"    ⚠  API Error (Attempt {attempt}/{max_retries}, "
                    f"status {status_code}): {err_str[:200]}"
                )
                time.sleep(min(delay, 30))

            if attempt >= max_retries:
                raise RuntimeError(
                    f"Claude API call failed after {attempt} attempts. Last error: {err_str}"
                )

        except Exception as e:
            err_str = str(e)
            if "FATAL" in err_str:
                raise
            print(f"    ⚠  Non-API Error (Attempt {attempt}/{max_retries}): {err_str[:200]}")
            time.sleep(min(30, base_delay * attempt))
            if attempt >= max_retries:
                raise RuntimeError(
                    f"Non-API failure after {attempt} attempts. Last error: {err_str}"
                )


# Back-compat alias: upstream stage code imports `call_gemini`. Keep working.
call_gemini = call_claude


def save_output(content, filename, output_dir):
    if content is None:
        print(f"  ⚠  WARNING: content is None for {filename}. Creating empty file.")
        content = ""
    with open(os.path.join(output_dir, filename), "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  ✓ Saved: {filename}")


def load_prompt(prompt_path):
    """Load a prompt file and inline the ``{{OUTPUT_FORMAT}}`` resource if referenced.

    A relative path of the form ``prompts/<name>`` resolves against the packaged
    prompts directory (overridable via ``PRESUBMIT_PROMPTS_DIR``), so pipeline
    callers can keep using short literals regardless of cwd.
    """
    path = Path(prompt_path)
    if not path.is_absolute() and path.parts and path.parts[0] == "prompts":
        path = prompts_dir().joinpath(*path.parts[1:])

    if not path.exists():
        return f"[Error: Prompt file {path} missing]"

    text = path.read_text(encoding="utf-8")

    if "{{OUTPUT_FORMAT}}" in text:
        output_format_path = prompts_dir() / "resources" / "output_format.txt"
        if output_format_path.exists():
            text = text.replace("{{OUTPUT_FORMAT}}", output_format_path.read_text(encoding="utf-8"))
        else:
            text = text.replace("{{OUTPUT_FORMAT}}", "")
    return text


def sanitize_pdf_ghostscript(input_path):
    if os.path.getsize(input_path) == 0:
        return input_path

    fd, fixed_path = tempfile.mkstemp(suffix=".pdf", prefix="sanitized_")
    os.close(fd)

    gs_cmd = shutil.which("gs")
    if gs_cmd:
        cmd = [gs_cmd, "-o", fixed_path, "-sDEVICE=pdfwrite", "-dPDFSETTINGS=/prepress", "-dQUIET", input_path]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(fixed_path) and os.path.getsize(fixed_path) > 0:
                return fixed_path
        except subprocess.CalledProcessError:
            pass

    try:
        reader = PdfReader(input_path)
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.write(fixed_path)
        return fixed_path
    except Exception:
        pass

    return input_path


def merge_pdfs_python(main_pdf, supplement_source, output_dir=None):
    if output_dir:
        output_path = os.path.join(output_dir, os.path.basename(main_pdf))
    else:
        output_path = main_pdf

    temp_path = output_path + ".tmp_merging"

    writer = PdfWriter()
    page_info = {"pages": []}

    supplement_pdfs = []
    if isinstance(supplement_source, list):
        supplement_pdfs = supplement_source
    elif isinstance(supplement_source, str) and os.path.isdir(supplement_source):
        files = sorted(os.listdir(supplement_source))
        supplement_pdfs = [os.path.join(supplement_source, f) for f in files if f.lower().endswith(".pdf")]

    try:
        reader = PdfReader(main_pdf)
        main_page_count = len(reader.pages)
        for page in reader.pages:
            writer.add_page(page)
        page_info["pages"].append({
            "name": "Main document",
            "pages": main_page_count,
            "numbering": "See main text page numbers",
            "pdf_start": 1,
            "pdf_end": main_page_count,
        })
    except Exception as e:
        print(f"  ✗ Error merging main PDF: {e}")
        return main_pdf, page_info

    current_pdf_page = main_page_count + 1

    if supplement_pdfs:
        sep_path = prompts_dir() / "resources" / "separator_supp.pdf"
        if sep_path.exists():
            try:
                sep_reader = PdfReader(str(sep_path))
                sep_count = len(sep_reader.pages)
                for page in sep_reader.pages:
                    writer.add_page(page)
                current_pdf_page += sep_count
            except Exception as e:
                print(f"  ⚠ Warning: Could not merge separator: {e}")
        else:
            print(f"  ⚠ Warning: Separator file not found at {sep_path}")

    for _i, supp_pdf in enumerate(supplement_pdfs, 1):
        try:
            reader = PdfReader(supp_pdf)
            supp_page_count = len(reader.pages)
            for page in reader.pages:
                writer.add_page(page)

            first_page_text = ""
            try:
                first_page_text = reader.pages[0].extract_text()
            except Exception:
                pass

            numbering = "Unknown"
            if re.search(r"\bS\d+\b", first_page_text):
                numbering = "S-numbered"
            elif re.search(r"\bAppendix\s+[A-Z]\b", first_page_text, re.IGNORECASE):
                numbering = "Appendix-lettered"

            filename = os.path.basename(supp_pdf)
            display_name = re.sub(r"^\d+_\d+_", "", filename)
            page_info["pages"].append({
                "name": display_name,
                "pages": supp_page_count,
                "numbering": numbering,
                "pdf_start": current_pdf_page,
                "pdf_end": current_pdf_page + supp_page_count - 1,
            })
            current_pdf_page += supp_page_count
        except Exception as e:
            print(f"  ⚠  Skipped corrupt supplement {supp_pdf}: {e}")

    with open(temp_path, "wb") as f:
        writer.write(f)
    if os.path.exists(output_path):
        os.remove(output_path)
    shutil.move(temp_path, output_path)
    return output_path, page_info
