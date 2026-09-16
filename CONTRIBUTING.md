# Contributing

Thanks for helping improve Usage Tracker.

## Development setup

1. Use Windows with Python 3 available.
2. Clone the repository and work from the repository root.
3. Start the app with `start.bat` or `start-background.ps1`.
4. Keep generated usage/account data local and untracked.

## Before submitting a change

Run:

```powershell
python -B -m unittest discover -v
node --check app.js
python -B -m py_compile server.py run_server.py
git diff --check
```

Add or update tests when changing parsing, quota normalization, model catalog logic, task classification, or persistence behavior.

## Privacy rules

Never commit real prompts, account identifiers, auth material, personal email addresses, private repository names, absolute user-specific paths, or generated runtime state. Use synthetic fixtures in tests and documentation.

## Pull requests

Keep changes focused, explain the user-visible behavior, and include the validation commands you ran. Avoid unrelated formatting or generated-data changes in the same pull request.
