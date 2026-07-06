"""On-disk state for `detect --follow`: the byte offsets already consumed
from the log, and the rolling window of recently seen records detectors
re-analyze on every poll.

Stored as `<log>.llm-burnwatch-follow-state.json`, a sibling of the log
path (not inside it, so a directory-mode log's own `*.jsonl` glob never
picks it up). Written atomically (`tempfile.mkstemp` + `os.replace`, the
same pattern already used by `pricing_import.import_pricing`) so a process
killed mid-write never leaves a half-written state file behind.

A missing, corrupted, or malformed-shape state file is never a fatal error
-- `load_follow_state` falls back to a fresh, empty state and (for the
corrupted/malformed case specifically, not a first-run missing file) warns
explicitly, the same graceful-degradation discipline already used for a
tampered ML model registry in `cli._run_ml_cross_check`.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from ._messages import warn


def state_path_for(log_path) -> Path:
    """`<log>.llm-burnwatch-follow-state.json`, next to `log_path`."""
    log_path = Path(log_path)
    return log_path.with_name(log_path.name + ".llm-burnwatch-follow-state.json")


def _empty_state() -> dict:
    return {"offsets": {}, "window": []}


def load_follow_state(state_path: Path) -> dict:
    """Load `{"offsets": {...}, "window": [...]}` from `state_path`.

    Returns a fresh empty state, with no warning, if the file simply
    doesn't exist yet (the expected first `--follow` run). Returns a fresh
    empty state *with* a warning if the file exists but is unreadable,
    corrupt JSON, or missing/mistyped its expected keys -- `--follow`
    starts over from the beginning of the log in that case rather than
    crash on a state file it can't trust.
    """
    if not state_path.exists():
        return _empty_state()

    try:
        with state_path.open("r", encoding="utf-8") as fh:
            state = json.load(fh)
        offsets = state["offsets"]
        window = state["window"]
        if not isinstance(offsets, dict) or not isinstance(window, list):
            raise ValueError("offsets must be an object and window a list")
        return {"offsets": offsets, "window": window}
    except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as exc:
        warn(
            f"could not read follow-state file {state_path} ({exc}); "
            "starting over from the beginning of the log"
        )
        return _empty_state()


def save_follow_state(state_path: Path, state: dict) -> None:
    """Atomically write `state` (`{"offsets", "window"}`) to `state_path`."""
    fd, tmp_path = tempfile.mkstemp(
        dir=state_path.parent, prefix=".llm-burnwatch-follow-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp_path, state_path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_path)
        raise
