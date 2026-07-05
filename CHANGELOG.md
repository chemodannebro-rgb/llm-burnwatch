# Changelog

All notable changes to this project are documented in this file.

## [0.5.0] - 2026-07-05

### Added
- `report --json`: prints a machine-readable summary (same shape as the
  human-readable output), matching the flag `detect` already had.
- `report --trace-id <id>`: narrows the report to the calls belonging to
  one request (e.g. a multi-step RAG turn logged under a shared
  `trace_id`), finally surfacing a field that was captured on every call
  but never read back anywhere.
- `train` now computes and prints a held-out self-consistency eval
  metric: a throwaway `IsolationForest` is fit on a deterministic ~80%
  split of the feature matrix and evaluated against the remaining ~20%
  it never saw, reporting what fraction of unseen examples it still
  flags as anomalous. The model actually saved to the registry is still
  fit on the full dataset afterward — the split only exists to produce
  an honest metric. Skipped (with an explicit reason) below 20 total
  training examples. Persisted in `metadata.json` as `eval_metrics`.
  `train()` now returns `(version_dir, eval_metrics)`.
- `cached_input_tokens` is now a feature evaluated by both the baseline
  z-score detector and the `IsolationForest` cross-check (previously
  logged but never scored by either detector) — closes a false-negative
  gap where a group starting to hit cache heavily would look anomalously
  cheap without anything actually being wrong.
- `hypothesis`-based property/fuzz tests for the baseline z-score
  statistics (`tests/test_baseline_properties.py`, new **dev-only**
  `hypothesis` dependency): random-input coverage of `_median_mad`/
  `_score_feature`/`analyze()` edge cases (all-identical values, MAD=0,
  single-sample groups, negative/extreme magnitudes) beyond the existing
  example-based tests.

### Changed
- `report` no longer materializes the whole log into a list before
  processing it — it now streams records through a generator
  (`_filter_report_records`) straight into `build_report()`, matching
  the already-streaming design of `iter_log_records()`. `detect`/
  `dashboard` are unchanged (out of scope: both need full group history
  in memory for cross-record statistics).
- `analyze()` computes each `(label, model)` group's median/MAD once per
  group instead of once per record scored against it (previously
  `O(group_size^2)` per group).

### Fixed
- `models/v1` (the committed example model registry) retrained to match
  the new 4-feature shape (adding `cached_input_tokens`); the old
  3-feature model would otherwise mismatch `IsolationForest.predict()`.

## [0.4.1] - 2026-07-05

### Added
- `detect --json`: each flagged feature now also includes a `reason`
  string — the same human-readable z-score/median/MAD explanation
  already printed by the non-JSON output (`baseline.format_score()`),
  so JSON consumers don't have to recompute it from the raw numbers.
- README: one-line mention that a `dashboard` command exists (a
  screenshot is intentionally deferred until the dashboard's next
  redesign).

### Fixed
- `detect`'s ML cross-check no longer treats a version pruned by a
  concurrent `train()` run as a hard failure: `latest_version_dir()`
  resolving a version right before a concurrent `train()` prunes it
  (e.g. `keep_last=1`) used to make that run's ML cross-check
  unavailable even though a newer, perfectly good model existed a
  moment later. `_run_ml_cross_check` now retries (bounded, 3 attempts)
  by re-resolving "latest" specifically on `FileNotFoundError`.

## [0.4.0] - 2026-07-05

### Added
- `dashboard` command: static, single-file HTML report (no JS, no CDN,
  no network calls). Central element is a **daily journal** — one
  collapsible `<details>` row per day (date, call count, cost, a
  fixed-width mini cost bar, top label, anomaly badge), expanding into
  that day's own by-label/by-model breakdown.
- `--since`/`--until` (`YYYY-MM-DD`, inclusive) on both `report` and
  `dashboard`, filtering by the record's UTC calendar date
  (`llmledger.logreader.filter_by_period`). The dashboard header shows
  the active period ("Period: all time" if neither is given).
- `SECURITY.md`: model registry trust boundary and vulnerability
  reporting process.
- `<meta name="viewport">` and a `@media (max-width: 600px)` layout for
  the daily journal, plus a `@media (prefers-color-scheme: dark)` theme
  mirroring the light palette.
- Test: `test_readme_log_format_section_mentions_all_schema_fields` —
  guards `schema.json`'s `properties` and README's "Log format" section
  against silently drifting apart.

### Fixed
- Removed the unbounded-width whole-log SVG chart (a quarter's worth of
  days made the chart wider than `<body>` and broke layout). Replaced
  with the fixed-size (100×14px) per-day mini bar described above —
  width is now constant regardless of log length.
- Mobile (`≤600px`) daily-journal row: an early implementation assigned
  the same CSS `grid-area` to four sibling elements (call count, mini
  bar, top label, anomaly badge), causing them to render stacked on top
  of each other instead of in visible rows. Rewritten as
  `display: flex; flex-wrap: wrap` with per-element `order`, so all
  fields stay legible and non-overlapping at narrow widths. Found and
  fixed via actual browser rendering/visual QA, not just structural
  (grep-based) HTML tests.

## [0.2.0] - 2026-07-03

### Added
- `--rub-rate` flag on `report`: shows total cost also converted to RUB
  at a fixed, manually-supplied rate (still no network calls).
- `log_gemini_response()` / `log_ollama_response()` adapters on
  `CostTracker`, following the same defensive `_get(x, name, 0) or 0`
  pattern as the existing OpenAI/Anthropic adapters.

### Changed
- **Breaking (model registry file format):** replaced `pickle` with
  `skops` in `anomaly/registry.py`. `skops` refuses by construction to
  construct any type outside its trusted-by-default list, removing the
  arbitrary-code-execution risk `pickle` carried on untrusted model
  files. The existing SHA256 integrity check is kept alongside it.
  `models/v1/` was retrained/recommitted in the new `model.skops`
  format.

## [0.1.0] - 2026-07-03

### Added
- Initial release: `CostTracker` (JSONL cost logging, log rotation via
  stdlib `RotatingFileHandler`, directory mode for multi-process
  writers) with OpenAI/Anthropic response adapters.
- Baseline anomaly detection: robust modified z-score (Iglewicz &
  Hoaglin) on `input_tokens`/`output_tokens`/`cost_micros` against the
  history of the same `(label, model)` pair — no third-party
  dependencies required.
- Optional ML cross-check: `IsolationForest` on group-relative
  features (`llmledger[anomaly]` extra), plus training-time vs.
  current per-group statistics drift detection.
- Versioned, SHA256-verified model registry (`models/vN/`); a working
  example registry committed at `models/v1/`.
- CLI: `report`, `demo-data`, `detect`, `train`, `schema`.
- JSONL log schema contract (`schema.json`, `llmledger schema`).
- CI (GitHub Actions): core-only smoke test + full test matrix
  (Python 3.9 & 3.12) with `[anomaly,dev]` extras, plus `pip-audit`.
