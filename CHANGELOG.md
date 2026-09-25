# Changelog

All notable public changes to Usage Tracker will be documented here.

## [Unreleased]

## [0.4.1] - 2026-09-25

### Fixed

- Corrected the task-completion-time chart to show a separate rolling mean for every `model + effort`, with persistent multi-model selection and a latest-window comparison table showing mean, median, and sample counts.
- Attribute completion time only to accepted single-model tasks with complete timestamps; exclude mixed-model and separately linked repair missions instead of presenting one misleading aggregate line.
- Label sparse windows and explain that time comparisons are not adjusted for task type or difficulty.

## [0.4.0] - 2026-09-25

### Added

- Rolling mean of measured active completion time for accepted Codex missions, with 30/90/180-day ranges, 7/14/30-day windows, sample counts, and explicit exclusion of between-turn waiting time.
- Four real dashboard captures in both READMEs, with explanations of weekly capacity, per-token quota efficiency, per-task quota consumption, and model token/cost trends; superseded duplicate images were removed from the README flow.
- Distinct Codex Fast model/effort variants, such as `5.6 sol xhigh (fast)`, throughout local token, quota, task-outcome, timeline, and model-breakdown data. Fast credit-equivalent estimates use the documented ChatGPT credit multiplier and are labeled separately from API charges.
- Codex account UUID snapshots outside the web root, conservative account attribution for new quota observations, per-account weekly-capacity chart selection, and evidence-labeled account/reset transitions. Older unattributed measurements remain separate.
- Historical Codex weekly quota-capacity chart in API-equivalent USD, with every measured interval, cumulative convergence, and selectable 7/14/30-day recent medians. Weekly rate-limit readings are backfilled from local session logs when the model cache upgrades.
- GPT-6 Sol and GPT-6 Luna API prices and published Codex Standard credit rates, shown separately from the tracker's normalized chart credits.
- Read-only discovery of visible GPT and ChatGPT Web model/effort choices from the local Codex model cache on each dashboard refresh. New choices without a verified price or benchmark remain explicitly unpriced and unranked.
- Daily background refresh of Artificial Analysis leaderboard scores, speed and cost per benchmark task through its official Free Data API when a server-side API key is configured; a last-good cache and dated offline snapshot remain available.
- Verified GPT-6 Sol and GPT-6 Luna Artificial Analysis release results in the offline leaderboard snapshot.

### Fixed

- Rename the dashboard and exported reports from the Antigravity-specific AGY brand to Usage Tracker; Gemini/Antigravity remains an explicit data source next to Codex.
- Translate dynamic quota-chart explanations, status lines, task-duration details, model summaries, reset clocks, and table controls when English is selected; switching languages rerenders live sections.
- Make `gpt-6-sol max` orange so its legend and plotted series are visually distinct from cyan `5.6 luna xhigh`.
- Rebuild both Codex log caches with service-tier attribution, preserve a turn's original tier when the setting changes mid-turn, and scan the complete local log ledger during migration.
- Preserve the GPT generation in Codex log attribution, so GPT-6 Sol/Luna usage no longer falls into GPT-5.6 Sol/Luna totals. Available legacy log files are rescanned when the attribution cache upgrades.
- Show locally observed models in the leaderboard's default scope, even if they are absent from the current picker.
- Prevent leaderboard local tokens and cost from counting Codex automatic logs a second time when those same logs are already included in Model Breakdown.
- Give observed model/effort combinations distinct, stable colors across token, cost, and 5-hour quota trend charts, including when chart ranges or rankings change.
- Explain that the quota-per-task trend only plots model/effort variants with enough valid tasks for both that variant and the Sol High baseline inside the same time window.
- Display small AA cost-per-task values without rounding them to $0.00 and calculate IQ/$ from the actual published cost rather than a hidden $0.05 floor.
- Let the local web server accept requests immediately; dashboard analysis starts when the page requests live data instead of blocking startup beyond the launcher timeout. The first successful live scan also refreshes the offline `data.js` fallback.

## [0.3.0] - 2026-09-22

### Added

- Resizable table viewports and persistent per-column widths across the dashboard.
- Grouped review filters for unresolved missions and soft-audit signals.
- Repair-chain accounting that keeps the repair model's own usage while charging the same quota cost as a penalty to the model whose result required the repair.
- Historical mission-boundary reasons and automatic separation of cross-model repairs, quota-accounting questions, and new actions after a model handoff.
- A complete catalog of the 27 currently selectable Codex model/effort combinations, while retaining locally observed models that are no longer in the picker.
- Empirical cross-model repair evidence with accepted successes, attempts, task categories, median 5-hour quota consumption, and confidence labels.

### Changed

- Reprocessed historical Codex missions with accent-aware repair detection, long-gap boundaries, and safer same-thread repair matching.
- A same-thread repair immediately after the failed mission is preferred; cross-thread repairs require an explicit task reference or manual review.
- Explicit resume prompts still continue the prior mission after a long interruption, while a substantial new request containing only a generic follow-up phrase starts a new mission.
- Mission comparisons prioritize measured 5-hour quota consumption; raw-token ratios remain supporting evidence with provenance and confidence labels.
- UI selections, chart ranges, language, table sizes, and column widths persist locally between launches.
- The public checkout uses port `5051`, with safer start/stop handling for stale PID files and unrelated port owners.
- Refreshed the Artificial Analysis benchmark snapshot to Index v4.3.2 (2026-09-21), with unbenchmarked selectable configurations shown explicitly instead of omitted.
- Kept Model Breakdown as observed per-model token and cost accounting; repair penalties remain isolated to task-outcome efficiency so actual spend is not double-counted.

### Fixed

- Prevented quota/cost questions from being mistaken for repair work.
- Prevented the live unfinished tail of the current answer from moving completed-mission matrix medians; the usage enters the matrix after the turn finishes.
- Classified questions about model ratios, quota penalties, and sudden efficiency changes as research instead of inheriting the Usage Tracker web-project label.
- Prevented phrases such as “do not use old data,” “CPU is not fully used,” or “not pushed to GitHub yet” from creating false repair links.
- Restored complete Vietnamese/English translation of dynamically rendered task-outcome controls and status labels.
- Restored complete Vietnamese/English translation of the model leaderboard and repair-evidence table.

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
