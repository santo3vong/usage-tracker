# Changelog

All notable public changes to Usage Tracker will be documented here.

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
