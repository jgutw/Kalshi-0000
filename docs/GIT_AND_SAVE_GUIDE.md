# When to Push to Git vs. Just Save (Ctrl-S)

## Push to Git when:
- **Code changes** — You've modified `.py` files, config defaults, or other source code
- **Logic fixes** — Bug fixes, new features, refactors that others (or future-you) should use
- **Config structure** — You've changed how `config.py` or other shared config works
- **Reusable changes** — Anything you'd want to restore or share across machines

## Don't push to Git (or remove from .gitignore) when:
- **Local runtime state** — `kalshi_sim.json` (balance, trades, returns) — changes every run, machine-specific
- **Secrets** — `.env`, API keys, credentials
- **Logs** — `logs/` directory, `*.jsonl` decision/trade logs — high churn, usually local
- **Generated/temp files** — `__pycache__/`, `venv/`, build artifacts

## Ctrl-S (Save) is for:
- Preserving edits in your editor before committing
- Never skip this before `git commit` — uncommitted changes can be lost

**Rule of thumb:** If it's *source code* or *shared configuration*, commit and push. If it's *your run's output* or *secrets*, keep it local and gitignored.
