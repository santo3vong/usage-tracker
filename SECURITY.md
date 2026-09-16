# Security Policy

## Reporting a vulnerability

Please avoid publishing exploit details or private user data in a public issue. Open a minimal issue describing the affected component and ask for a private reporting channel, or use the repository's private security-advisory feature when available.

## Sensitive local data

Usage Tracker reads local AI-assistant state and can encounter prompts, account identifiers, local filesystem paths, usage history, and quota information. Generated files listed in `.gitignore` must remain untracked.

Before publishing a fork, release, bug report, screenshot, or diagnostic bundle, review it for credentials, personal identifiers, prompt history, private repository names, and user-specific absolute paths.

Usage Tracker does not need users to paste API keys into the repository. Do not add secrets to source files, test fixtures, screenshots, or issue reports.
