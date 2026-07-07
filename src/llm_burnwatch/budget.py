"""Persisted user budget config for `llm-burnwatch budget set`/`show` and
`BudgetDetector`: a monthly USD budget and a warn-at fraction, written to
`budget.json` in the user's XDG config directory (`tracker.user_budget_path`),
next to `pricing.json`.

Written atomically (`tempfile.mkstemp` + `os.replace`, the same pattern
already used by `pricing_import.import_pricing` and
`follow_state.save_follow_state`) so a process killed mid-write never leaves
a half-written budget file behind.

A missing budget.json is not an error -- `load_budget` returns `None`, and
callers (`BudgetDetector`, `report`, `budget show`) treat that as "budget
tracking not configured", staying silent by default rather than nagging an
operator who never asked for this feature (the same precedent as an
unconfigured `RulesDetector`). A corrupted/malformed budget.json *is*
reported via `warn()`, then treated the same as "not configured" --
`detect`/`report` should never crash because of a hand-edited config file
(same graceful-degradation discipline as `follow_state.load_follow_state`).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from ._messages import warn


def load_budget(path: Path) -> dict | None:
    """Load `{"monthly_usd": float, "warn_at_fraction": float}` from `path`.

    Returns `None`, with no warning, if `path` simply doesn't exist (budget
    tracking was never configured -- the expected state before `budget set`
    has ever been run). Returns `None` *with* a warning if the file exists
    but is unreadable, corrupt JSON, or missing/mistyped its expected keys.
    """
    if not path.exists():
        return None

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        monthly_usd = data["monthly_usd"]
        warn_at_fraction = data["warn_at_fraction"]
        if not isinstance(monthly_usd, (int, float)) or isinstance(monthly_usd, bool):
            raise ValueError("monthly_usd must be a number")
        if not isinstance(warn_at_fraction, (int, float)) or isinstance(warn_at_fraction, bool):
            raise ValueError("warn_at_fraction must be a number")
        return {"monthly_usd": float(monthly_usd), "warn_at_fraction": float(warn_at_fraction)}
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        warn(f"could not read budget file {path} ({exc}); treating budget as not configured")
        return None


def save_budget(path: Path, monthly_usd: float, warn_at_fraction: float) -> None:
    """Atomically write `{"monthly_usd", "warn_at_fraction"}` to `path`,
    creating parent directories as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".budget-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"monthly_usd": monthly_usd, "warn_at_fraction": warn_at_fraction}, fh, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
        raise
