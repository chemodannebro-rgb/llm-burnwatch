# Quickstart

llm-burnwatch tracks the cost of every LLM/agent call your code makes,
writes it to a plain JSONL file on your own disk, and flags anomalies —
runaway loops, cost spikes, model swaps — before they show up as a
surprise on next month's bill.

**In short:** zero required dependencies, no network calls in its core,
one file you can open and read yourself. See [Security model](security.md)
for exactly what that guarantee covers and where the opt-in exceptions are.

## Install

```bash
pip install llm-burnwatch
```

## Log your first call

```python
from llm_burnwatch import CostTracker

tracker = CostTracker("calls.jsonl")
tracker.log_call(
    label="summarize",
    model="gpt-4o-mini",
    input_tokens=800,
    output_tokens=150,
)
```

Already calling an OpenAI, Anthropic, Gemini, Ollama, or LangChain SDK?
Skip computing tokens by hand — there's a one-line adapter for each. See
[Connecting to an existing app](connecting.md).

## See what it cost

```bash
llm-burnwatch report --log-file calls.jsonl
```

## Get your first alert

You don't need real traffic to see detection work. Generate a synthetic
log with a few injected anomalies, and find them:

```bash
llm-burnwatch demo-data --out demo.jsonl
llm-burnwatch detect --log-file demo.jsonl
```

`detect` exits `1` if it found anything — anomalies, rule violations,
frequency spikes, level shifts, budget alerts — and `0` otherwise, so it
drops straight into CI or a cron job. For a version that keeps running and
pushes alerts to Slack, Telegram, a webhook, or a local command as they
happen, see `detect --follow` in the [Public API](api.md#cli) reference.

## See it as a dashboard

```bash
llm-burnwatch dashboard --log-file demo.jsonl --out dashboard.html
open dashboard.html
```

One self-contained HTML file. No server, no build step, works straight
from `file://`. The small amount of interactivity (sortable tables,
copy-to-clipboard) is inline vanilla JS — no CDN, no network call. See
[Security model](security.md).

## Where to go next

| I want to... | Read this |
|---|---|
| Connect this to an app that already calls an LLM SDK | [Connecting to an existing app](connecting.md) |
| Know exactly what each detector catches and how to tune it | [Detectors](detectors/baseline.md) |
| Choose between `budget` and `guard()` | [`budget` vs `guard()`](budget-vs-guard.md) |
| Know exactly what data ever leaves my machine | [Security model](security.md) |
| See every command, flag, and `--json` key | [Public API](api.md) |
| Decide if this is even the right tool | [Comparison](comparison.md) |
| Find an answer to a question not covered here | [FAQ](faq.md) |
