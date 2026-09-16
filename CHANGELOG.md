# Changelog

All notable public changes to Usage Tracker will be documented here.

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
