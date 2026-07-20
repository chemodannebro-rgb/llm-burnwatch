"""Pure logic behind `llm-burnwatch init` -- a non-interactive, non-file-writing
quickstart: detect which LLM SDK(s) are installed and print the matching
`docs/connecting.md` snippet, then (if an existing log has enough history)
suggest `--max-call-cost`/monthly-budget numbers based on actual usage.

`init` never writes anything to disk itself -- unlike `budget set`/`pricing
import`, it only prints ready-to-copy commands for the user to run
themselves, so it opens no new trust boundary (see SECURITY.md). SDK
detection uses `importlib.util.find_spec`, never `import`, so it has no
side effects and works even if a found package would itself fail to import
for unrelated reasons (missing env vars, etc.) -- see `detect_available_sdks`.

`SDK_SNIPPETS` below is a verbatim copy of the code blocks in
`docs/connecting.md`'s "SDK adapters" section -- if that section changes,
update this dict to match (no automated sync exists between the two).
"""

from __future__ import annotations

import importlib.util
import math
from typing import Sequence

from .logreader import parse_timestamp

# Adapter name (matches the `log_<name>_response`/`log_<name>_result` suffix
# in tracker.py) keyed by the Python module `importlib.util.find_spec` would
# locate for that SDK -- checking the import path, not the PyPI package name
# (e.g. the `google-genai` package imports as `google.genai`).
KNOWN_SDKS: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "google.genai": "gemini",
    "ollama": "ollama",
    "langchain_core": "langchain",
}

SDK_SNIPPETS: dict[str, str] = {
    "openai": (
        "response = openai_client.chat.completions.create(...)\n"
        'tracker.log_openai_response(response, label="chat")'
    ),
    "anthropic": (
        "response = anthropic_client.messages.create(...)\n"
        'tracker.log_anthropic_response(response, label="chat")'
    ),
    "gemini": (
        "response = gemini_client.models.generate_content(...)\n"
        'tracker.log_gemini_response(response, label="chat")'
    ),
    "ollama": (
        "# local models usually have no pricing.json entry, so pass cost=0.0\n"
        "# (or your own pricing=); only pass the final chunk if you're streaming.\n"
        "response = ollama_client.chat(...)\n"
        'tracker.log_ollama_response(response, label="chat", cost=0.0)'
    ),
    "langchain": (
        "# reads AIMessage.usage_metadata (current langchain-core), or falls\n"
        '# back to the older LLMResult.llm_output["token_usage"] shape.\n'
        "result = chat_model.invoke(...)\n"
        'tracker.log_langchain_result(result, label="chat")'
    ),
}

# Below this many calls, or this few calendar days of history, a suggested
# --max-call-cost/monthly-budget number would be based on too little data to
# be meaningful -- same "insufficient data, say so, don't guess" precedent as
# `anomaly/seasonal.py`'s MIN_SEASONAL_SPAN_DAYS gate.
INIT_SUGGESTION_MIN_DAYS = 7
INIT_SUGGESTION_MIN_CALLS = 20


def detect_available_sdks() -> list[str]:
    """Adapter names (e.g. `"openai"`) for every `KNOWN_SDKS` module that
    `importlib.util.find_spec` locates on `sys.path`. Never imports the
    module itself -- `find_spec` only consults path/metadata, so this has no
    side effects and can't fail even if the package would itself raise on
    import (e.g. a missing native extension or API key check at import
    time). Returns adapter names in `KNOWN_SDKS`' insertion order.
    """
    found = []
    for module_name, adapter_name in KNOWN_SDKS.items():
        try:
            spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError, ModuleNotFoundError):
            # A parent package that itself fails to import (e.g. a stub or
            # broken partial install) surfaces as one of these from
            # find_spec -- treat exactly like "not found" rather than
            # letting `init` crash over an unrelated, already-broken SDK.
            spec = None
        if spec is not None:
            found.append(adapter_name)
    return found


def compute_init_suggestions(records: Sequence[dict]) -> dict | None:
    """Usage-based suggestions for `--max-call-cost`/monthly `budget set`,
    or `None` if `records` has fewer than `INIT_SUGGESTION_MIN_CALLS` calls
    or spans fewer than `INIT_SUGGESTION_MIN_DAYS` calendar days -- too
    little data for a suggestion to mean anything.

    - `suggested_max_call_cost_usd`: 99th-percentile per-call cost, x10.
      Nearest-rank (no interpolation) rather than `statistics.quantiles`,
      to avoid a new dependency and interpolation choices for what is a
      one-off, order-of-magnitude suggestion, not a precise statistic --
      x10 leaves headroom for the naturally larger of your normal calls to
      not trip a threshold set from the same history.
    - `suggested_monthly_budget_usd`: total spend over the log's span,
      projected to a 30-day month (mirrors `compute_budget_status`'s own
      day-rate forecast, not a calendar-month boundary).
    """
    if len(records) < INIT_SUGGESTION_MIN_CALLS:
        return None

    timestamps = [
        ts for ts in (parse_timestamp(r.get("timestamp")) for r in records) if ts is not None
    ]
    if len(timestamps) < 2:
        return None
    days_spanned = (max(timestamps) - min(timestamps)).total_seconds() / 86400
    if days_spanned < INIT_SUGGESTION_MIN_DAYS:
        return None

    sorted_costs = sorted(r.get("cost_micros", 0) for r in records)
    idx = min(len(sorted_costs) - 1, math.ceil(0.99 * len(sorted_costs)) - 1)
    p99_micros = sorted_costs[idx]

    total_micros = sum(sorted_costs)

    return {
        "days_spanned": round(days_spanned),
        "call_count": len(records),
        "models_seen": sorted({r["model"] for r in records if r.get("model")}),
        "suggested_max_call_cost_usd": round(p99_micros * 10 / 1_000_000, 2),
        "suggested_monthly_budget_usd": round(
            total_micros / days_spanned * 30 / 1_000_000, 2
        ),
    }
