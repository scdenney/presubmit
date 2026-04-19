"""openpeerreview — adversarial peer-review pipeline for academic PDFs, on Claude.

Ported from upstream `reviewer2` (Apache-2.0, isitcredible/reviewer2).
See NOTICE for attribution and README for Claude-specific trade-offs.
"""

from openpeerreview.core import call_claude, call_gemini, cleanup_resources
from openpeerreview.pipeline import PipelineError, run
from openpeerreview.render_text import render_text

__version__ = "0.1.0"
__all__ = [
    "call_claude",
    "call_gemini",  # back-compat alias for ported stage code
    "cleanup_resources",
    "render_text",
    "run",
    "PipelineError",
    "__version__",
]
