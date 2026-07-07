"""End-to-end walkthrough of the v0.9 "actions and integrations" pipeline:

    CostTracker.log_langchain_result() (0.9.5)
        -> a real budget.json + BudgetDetector (0.9.2)
        -> `detect --follow` (0.9.1's registry, 0.9.3's guard is orthogonal
           and not exercised here -- see its own docstring in tracker.py)
        -> a real WebhookSink (0.9.1) delivering alerts over an actual
           HTTP POST, to a real local server started just for this script.

Unlike `full_pipeline.py`, this isn't a "core always works, ML extra is
optional" walkthrough -- every step here is core, zero-extra functionality.
What makes it worth a separate script is that it exercises the *actions*
half of llm-burnwatch (sinks, budget, follow) with a genuine local HTTP
server as the webhook receiver, rather than the mocked `urlopen` every
`test_sinks_webhook.py`/`test_detect_follow.py` test uses -- so this is the
one place in the repo that proves the webhook sink's HTTP POST actually
round-trips over a real socket, end to end.

`detect --follow` is an intentionally infinite polling loop (see
`cli.py`'s `_run_detect_follow`), so -- like the tests in
`test_detect_follow.py` -- this script patches `time.sleep` to raise
`KeyboardInterrupt` after exactly one poll, which `_run_detect_follow`
already catches and turns into a clean return. That's a deliberate reuse of
the same trick the test suite uses, not a new mechanism: this script is a
smoke-test/demo, not a pytest test, precisely because `--follow`'s infinite
loop isn't something a real integration test should run forever either.

    python examples/e2e_actions_demo.py
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from llm_burnwatch import budget
from llm_burnwatch import cli as cli_module
from llm_burnwatch.tracker import CostTracker, user_budget_path


class _WebhookReceiver(BaseHTTPRequestHandler):
    """Minimal real HTTP server standing in for whatever alerting endpoint
    an operator would actually point `--webhook-url` at (Slack, PagerDuty,
    a custom incident bot, ...). Records every alert it receives on the
    class itself so `main()` can inspect them after the poll loop exits.
    """

    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler's name)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.received.append(json.loads(body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:  # silence per-request logging
        pass


def main() -> None:
    work_dir = Path(tempfile.mkdtemp(prefix="llm-burnwatch-e2e-actions-"))
    log_file = work_dir / "calls.jsonl"

    # `user_budget_path()`/`user_pricing_path()` resolve under
    # `$XDG_CONFIG_HOME/llm-burnwatch/` -- pointing that at this script's own
    # temp dir keeps the demo from touching the real machine's config.
    os.environ["XDG_CONFIG_HOME"] = str(work_dir / "config")

    # 1. Start a real local webhook receiver on an OS-assigned port -- this
    #    is genuinely a second process-local socket, not a mock.
    server = HTTPServer(("127.0.0.1", 0), _WebhookReceiver)
    webhook_url = f"http://127.0.0.1:{server.server_port}/alerts"
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    print(f"webhook receiver listening at {webhook_url}")

    # 2. Configure a monthly budget via the same `budget.save_budget()`
    #    `llm-burnwatch budget set` itself calls -- $1.00/month, warn at 50%.
    budget.save_budget(user_budget_path(), monthly_usd=1.00, warn_at_fraction=0.5)
    print(f"budget configured: {user_budget_path()}")

    # 3. Log a few expensive calls through the new 0.9.5 LangChain adapter.
    #    A fake AIMessage-shaped dict is enough -- `log_langchain_result()`
    #    reads `usage_metadata` via `_get()`, so a plain dict works exactly
    #    like a real `AIMessage` would. gpt-4o pricing ($2.50/1M input,
    #    $10/1M output) makes each of these three calls ~$0.45, so three
    #    calls push month-to-date spend to ~$1.35 -- comfortably past the
    #    $1.00 budget regardless of what day of the month this actually
    #    runs on (`BudgetDetector.over_budget` compares month-to-date spend
    #    directly, it isn't a forecast).
    tracker = CostTracker(log_file)
    for i in range(3):
        fake_ai_message = {
            "usage_metadata": {"input_tokens": 100_000, "output_tokens": 20_000},
            "response_metadata": {"model_name": "gpt-4o"},
        }
        tracker.log_langchain_result(fake_ai_message, label="demo-agent-turn")
        print(f"logged call {i + 1}/3 via log_langchain_result()")

    # 4. Run one `detect --follow` poll against that log, with a low
    #    `--max-call-cost` so each of those ~$0.45 calls also trips
    #    `RulesDetector` (a per-call rule, deterministic regardless of
    #    history -- unlike the statistical detectors, which need a longer
    #    baseline than three calls to say anything meaningful), and
    #    `--webhook-url` pointed at the receiver started in step 1.
    #    `_run_detect_follow`'s loop is intentionally infinite; patching
    #    `time.sleep` to raise `KeyboardInterrupt` bounds it to exactly one
    #    poll, the same trick `test_detect_follow.py` already uses.
    argv = [
        "detect",
        "--log-file",
        str(log_file),
        "--max-call-cost",
        "0.10",
        "--follow",
        "--webhook-url",
        webhook_url,
    ]
    original_sleep = cli_module.time.sleep
    cli_module.time.sleep = lambda seconds: (_ for _ in ()).throw(KeyboardInterrupt)
    try:
        exit_code = cli_module.main(argv)
    finally:
        cli_module.time.sleep = original_sleep
    print(f"\n`detect --follow` poll finished with exit code {exit_code}")

    # 5. Give the receiver's background thread a moment to finish handling
    #    whatever's already in its socket backlog, then report what a real
    #    operator's webhook endpoint would have actually received.
    server.shutdown()
    server_thread.join()

    print(f"\nwebhook receiver got {len(_WebhookReceiver.received)} alert(s):")
    for alert in _WebhookReceiver.received:
        print(f"  - [{alert['severity']}] {alert['detector']}/{alert['kind']}: {alert['message']}")

    kinds = {a["kind"] for a in _WebhookReceiver.received}
    assert "call_cost_exceeded" in kinds, "expected a RulesDetector per-call alert"
    assert "budget_exceeded" in kinds, "expected a BudgetDetector over-budget alert"
    print("\nconfirmed: both a per-call rule alert and a budget alert were "
          "delivered over a real HTTP POST to the webhook receiver.")


if __name__ == "__main__":
    main()
