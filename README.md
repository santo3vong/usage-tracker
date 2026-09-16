# Usage Tracker

Usage Tracker is a local-first dashboard for inspecting AI coding-assistant usage on a Windows workstation. It combines local transcript/session data with live quota information when supported, then summarizes tokens, estimated cost, tool activity, model usage, quota windows, and task outcomes in a browser dashboard.

The project is designed for local analysis. Generated usage history, account state, quota observations, and dashboard data stay on the machine and are excluded from Git by default.

## What it reads

- Codex local session/transcript data.
- Codex app-server rate-limit RPC when a compatible local Codex installation is available.
- Gemini/Antigravity local transcript and account metadata when detected.

Usage Tracker does not require a hosted backend. The UI currently loads Google Fonts from the public Google Fonts CDN; account avatar URLs may also be displayed when discovered from local account metadata.

## Requirements

- Windows 10 or Windows 11.
- Python 3. Usage Tracker will use the Python runtime bundled with Codex when available, then fall back to `py -3` or `python`.
- A modern browser.
- Node.js is optional and only needed for the JavaScript syntax check used during development/CI.

The current launch scripts are Windows-first. Other platforms are not yet an advertised or tested target.

## Quick start

From the repository root:

```powershell
.\start.bat
```

Or start it without opening a browser automatically:

```powershell
powershell -ExecutionPolicy Bypass -File .\start-background.ps1
```

Then open `http://127.0.0.1:5050/`.

To stop or restart the local server:

```powershell
powershell -ExecutionPolicy Bypass -File .\stop-server.ps1
powershell -ExecutionPolicy Bypass -File .\restart-server.ps1
```

Runtime PID and server logs are written under `runtime/`.

## Tests

Run the full local validation set with:

```powershell
python -B -m unittest discover -v
node --check app.js
python -B -m py_compile server.py run_server.py
```

GitHub Actions runs the same core checks on Windows.

## Local data and privacy

The following generated/local files are intentionally ignored by Git:

- `accounts.json`
- `codex_usage.json`
- `codex_models_cache.json`
- `codex_mission_turns_cache.json`
- `codex_mission_reviews.json`
- `quota_observations.json`
- `real_quotas.json`
- `time_series_history.json`
- `data.js`
- `runtime/` and `runtime_backups/`

These files can contain account identifiers, prompts, local paths, usage history, and other private machine data. Do not force-add them to a public commit.

## Accuracy and limitations

Token and usage values derived directly from local records are reported from the available source data. Cost and quota-capacity figures may include estimates or inferred values where a provider does not expose an authoritative value. Treat those estimates as diagnostic aids rather than billing records.

The task-outcome classifier is heuristic and intended for local analysis, not as an authoritative benchmark of model quality.

## Contributing

See `CONTRIBUTING.md` for development and pull-request guidance. Security and privacy issues should follow `SECURITY.md`.

## License

MIT. See `LICENSE`.
