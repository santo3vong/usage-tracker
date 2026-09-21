# Changelog

All notable public changes to Usage Tracker will be documented here.

## [Unreleased]

### Added

- Resizable table viewports and persistent per-column widths across the dashboard.
- Grouped review filters for unresolved missions and soft-audit signals.
- Repair-chain accounting that keeps the repair model's own usage while charging the same quota cost as a penalty to the model whose result required the repair.
- Historical mission-boundary reasons and automatic separation of cross-model repairs, quota-accounting questions, and new actions after a model handoff.

### Changed

- Reprocessed historical Codex missions with accent-aware repair detection, long-gap boundaries, and safer same-thread repair matching.
- A same-thread repair immediately after the failed mission is preferred; cross-thread repairs require an explicit task reference or manual review.
- Explicit resume prompts still continue the prior mission after a long interruption, while a substantial new request containing only a generic follow-up phrase starts a new mission.
- Mission comparisons prioritize measured 5-hour quota consumption; raw-token ratios remain supporting evidence with provenance and confidence labels.
- UI selections, chart ranges, language, table sizes, and column widths persist locally between launches.
- The public checkout uses port `5051`, with safer start/stop handling for stale PID files and unrelated port owners.

### Fixed

- Prevented quota/cost questions from being mistaken for repair work.
- Prevented the live unfinished tail of the current answer from moving completed-mission matrix medians; the usage enters the matrix after the turn finishes.
- Classified questions about model ratios, quota penalties, and sudden efficiency changes as research instead of inheriting the Usage Tracker web-project label.
- Prevented phrases such as “do not use old data,” “CPU is not fully used,” or “not pushed to GitHub yet” from creating false repair links.
- Restored complete Vietnamese/English translation of dynamically rendered task-outcome controls and status labels.

## [0.2.1] - 2026-09-16

### Documentation

- Split the README into real Vietnamese and English files with one-click language switching.
- Rewrote the project story around the practical problems that led to each feature before introducing implementation terms.
- Explained why current/instantaneous usage and typical/average usage need to be read together.
- Added the workload-dependent Sol orchestrator + Luna executor example instead of assuming a universal saving ratio.
- Explained common accounting mistakes such as summing cumulative counters, ignoring repair/re-teaching turns, mis-crediting mixed routes, or turning unknown values into zero.
- Added six privacy-safe UI snapshots built entirely from synthetic demo data.

## [0.2.0] - 2026-09-16

### Added

- Vietnamese/English interface switching for the dashboard, including dynamic UI elements.
- Bilingual public documentation and clearer provenance/accuracy notes.
- Documentation for mission grouping, task-outcome review, quota efficiency, quota-per-task analysis, and conservative sample handling.

## [0.1.0] - 2026-09-16

### Added

- Local dashboard for usage, token, cost, model, tool, quota, and task-outcome analysis.
- Codex local-session ingestion and live app-server rate-limit support with local-log fallback.
- Gemini/Antigravity local-source support where available.
- Windows launch, stop, and restart helpers.
- Unit-test coverage for usage sources, quota estimation, model catalog behavior, live Codex rate limits, and task outcomes.

### Security and privacy

- Runtime/account/history files are excluded from Git.
- Public defaults use neutral account placeholders rather than machine-specific identity data.
- Project-specific task-classification keywords were generalized for public release.
