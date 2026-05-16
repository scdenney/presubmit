"""Command-line interface for presubmit."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_SUPPORTED_INPUT_EXTS = (".pdf", ".md", ".markdown", ".txt", ".tex")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="presubmit",
        description="Claude-powered adversarial peer review for academic PDFs.",
    )
    parser.add_argument(
        "paper",
        help="Path to the paper. Accepts .pdf (the renderered article — text "
             "stages still get a markdown version internally for cheaper "
             "tokens), .md / .markdown / .txt (used as-is for every stage; "
             "vision-required stages fall back to the markdown), or .tex "
             "(converted to markdown via pandoc on entry).",
    )
    parser.add_argument(
        "-o", "--output", default="report.txt",
        help="Output text file path. Default: report.txt",
    )

    parser.add_argument(
        "--math", action="store_true",
        help="Enable the math-audit stages. Requires MATHPIX_APP_ID and MATHPIX_APP_KEY.",
    )
    parser.add_argument(
        "--code-dir", default=None, metavar="PATH",
        help="Path to a directory of replication source code. Passing this "
             "flag enables the code-audit add-on (off by default).",
    )
    parser.add_argument(
        "--no-copyedit", action="store_true",
        help="Skip the copyedit stages (proofreading and revision suggestions).",
    )
    parser.add_argument(
        "--no-editor-note", action="store_true",
        help="Skip the editor's-note stage (the Alchemist's revision advice). "
             "In this release, copyedit and editor's note are coupled: disabling "
             "either skips all Writer-Mode stages.",
    )
    parser.add_argument(
        "--base", action="store_true",
        help="Base review only. Disables every add-on: math, code, copyedit, editor's note.",
    )
    parser.add_argument(
        "--supp", action="append", default=[], metavar="PDF",
        help="Path to a supplementary PDF to merge after the main paper. Repeatable.",
    )
    parser.add_argument(
        "--citation", default="",
        help="Manual citation string, used only if metadata extraction fails to produce one.",
    )

    parser.add_argument(
        "--work-dir", default=None,
        help="Directory for intermediate stage outputs and the consolidated report. "
             "Resume semantics: re-running with the same --work-dir picks up from the "
             "last successful stage. If unset, falls back to PRESUBMIT_OUTPUT_BASE/"
             "<slug>/presubmit_run/ when that env var is defined; otherwise to a temp "
             "dir (with a warning, since stage outputs are then garbage-collected).",
    )
    parser.add_argument(
        "--keep-work-dir", action="store_true",
        help="Keep the intermediate stage outputs after the run completes.",
    )
    parser.add_argument(
        "--skip-size-check", action="store_true",
        help="Bypass the 500-page combined-volume circuit breaker.",
    )
    parser.add_argument(
        "--start-stage", type=float, default=0.0, metavar="N.N",
        help="Resume from this stage number (e.g. 1.6 to skip metadata + most of "
             "the Red Team). 0.0 runs from the beginning. Cached stage files in "
             "--work-dir are skipped automatically; --start-stage is for forcing "
             "the pipeline past gates that don't correspond to a single .txt.",
    )
    parser.add_argument(
        "--stop-stage", type=float, default=100.0, metavar="N.N",
        help="Halt before this stage number (e.g. 4.0 to stop after the reviewer "
             "synthesis but before writer mode). 100.0 runs to end.",
    )
    return parser


def resolve_addons(args: argparse.Namespace) -> dict[str, bool]:
    """Collapse CLI flags into a stage-enable map matching pipeline.run kwargs."""
    if args.base:
        return {"math": False, "code": False, "copyedit": False, "editor_note": False}
    return {
        "math": args.math,
        "code": bool(args.code_dir),
        "copyedit": not args.no_copyedit,
        "editor_note": not args.no_editor_note,
    }


def _require_env(var: str, because: str) -> None:
    if not os.environ.get(var):
        print(f"error: {var} not set. Required {because}.", file=sys.stderr)
        sys.exit(2)


def _derive_slug(paper_path: Path) -> str:
    """Slugify a paper's filename.

    Lowercases the stem, collapses runs of non-alphanumeric characters
    (other than underscore) into single hyphens, strips trailing
    hyphens/underscores. Underscores inside the stem are preserved so a
    filename like ``Denney_2026_What-Were-They-Thinking.pdf`` round-trips
    to ``denney_2026_what-were-they-thinking``.
    """
    stem = paper_path.stem
    slug = re.sub(r"[^A-Za-z0-9_]+", "-", stem)
    slug = re.sub(r"-+", "-", slug).lower().strip("-_")
    return slug or "paper"


def _resolve_work_dir(args: argparse.Namespace, paper_path: Path) -> tuple[Path, bool]:
    """Resolve the work directory for this run.

    Precedence:
      1. ``--work-dir`` explicit (always wins, never auto-cleaned).
      2. ``$PRESUBMIT_OUTPUT_BASE`` env var → ``<base>/<slug>/presubmit_run/``.
      3. Temp dir (current default; print a discoverability warning).

    Returns ``(work_dir, cleanup_after_success)``.
    """
    if args.work_dir:
        wd = Path(args.work_dir).expanduser().resolve()
        wd.mkdir(parents=True, exist_ok=True)
        return wd, False

    base_env = os.environ.get("PRESUBMIT_OUTPUT_BASE", "").strip()
    if base_env:
        base = Path(base_env).expanduser().resolve()
        slug = _derive_slug(paper_path)
        wd = base / slug / "presubmit_run"
        wd.mkdir(parents=True, exist_ok=True)
        print(f"  → PRESUBMIT_OUTPUT_BASE → {wd}")
        return wd, False

    wd = Path(tempfile.mkdtemp(prefix="presubmit_"))
    print(
        f"  ⚠  No --work-dir or PRESUBMIT_OUTPUT_BASE set. Stage outputs will land in a\n"
        f"     temp dir ({wd}) and may be cleaned up after this run. To keep them, either\n"
        f"       (a) pass --work-dir <path> explicitly, or\n"
        f"       (b) set, e.g. `export PRESUBMIT_OUTPUT_BASE=~/presubmit-reviews` in your shell rc.",
        file=sys.stderr,
    )
    return wd, not args.keep_work_dir


def _convert_tex_to_md(tex_path: Path) -> Path:
    """Convert a .tex file to markdown via pandoc, into a temp file.

    Tries to inline citations against a sibling .bib (same stem, or a .bib
    in the same directory) when available. Raises SystemExit with a useful
    message if pandoc isn't on PATH.
    """
    if shutil.which("pandoc") is None:
        print(
            "error: .tex input requires `pandoc` on PATH "
            "(brew install pandoc / apt install pandoc / pacman -S pandoc).",
            file=sys.stderr,
        )
        sys.exit(2)

    out_md = Path(tempfile.mkstemp(prefix="presubmit_tex_", suffix=".md")[1])
    cmd = ["pandoc", str(tex_path), "-t", "gfm", "-o", str(out_md)]

    sibling_bib = tex_path.with_suffix(".bib")
    if sibling_bib.exists():
        cmd[1:1] = ["--citeproc", "--bibliography", str(sibling_bib)]
    else:
        # try any .bib in the same directory
        bibs = list(tex_path.parent.glob("*.bib"))
        if bibs:
            cmd[1:1] = ["--citeproc", "--bibliography", str(bibs[0])]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"error: pandoc conversion failed: {e.stderr.decode(errors='replace')[:500]}",
              file=sys.stderr)
        sys.exit(2)

    print(f"  ✓ Pandoc → {out_md} ({out_md.stat().st_size:,} bytes).")
    return out_md


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    paper_path = Path(args.paper).expanduser()
    if not paper_path.exists():
        print(f"error: input not found: {paper_path}", file=sys.stderr)
        return 2

    ext = paper_path.suffix.lower()
    if ext not in _SUPPORTED_INPUT_EXTS:
        print(
            f"error: unsupported input format '{ext}'. "
            f"Accepted: {', '.join(_SUPPORTED_INPUT_EXTS)}.",
            file=sys.stderr,
        )
        return 2

    if ext == ".tex":
        paper_path = _convert_tex_to_md(paper_path)
        ext = ".md"

    _require_env("ANTHROPIC_API_KEY", "for the Anthropic API")
    addons = resolve_addons(args)
    if addons["math"]:
        if ext != ".pdf":
            print(
                "error: --math requires .pdf input (mathpix and equation "
                "extraction need page rasters).",
                file=sys.stderr,
            )
            return 2
        _require_env("MATHPIX_APP_ID", "for the --math add-on")
        _require_env("MATHPIX_APP_KEY", "for the --math add-on")
    output_path = Path(args.output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    work_dir, cleanup_work_dir = _resolve_work_dir(args, paper_path)

    # Defer import to keep --help fast and avoid pulling in genai unnecessarily.
    from presubmit.pipeline import PipelineError, run

    try:
        final_txt = run(
            pdf_path=paper_path,
            work_dir=work_dir,
            math=addons["math"],
            code=addons["code"],
            copyedit=addons["copyedit"],
            editor_note=addons["editor_note"],
            supp_pdfs=args.supp or None,
            code_dir=args.code_dir,
            citation=args.citation,
            start_stage=args.start_stage,
            stop_stage=args.stop_stage,
            skip_size_check=args.skip_size_check,
        )
    except PipelineError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naborted by user", file=sys.stderr)
        return 130

    if final_txt and Path(final_txt).is_file():
        shutil.copy2(final_txt, output_path)
        print(f"\n✓ Report written to {output_path}")
    else:
        print(f"\nerror: pipeline did not produce a final report (stopped at {final_txt})", file=sys.stderr)
        return 1

    if cleanup_work_dir:
        shutil.rmtree(work_dir, ignore_errors=True)
    elif not args.work_dir:
        print(f"  (intermediate outputs kept at {work_dir})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
