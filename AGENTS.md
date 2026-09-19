# Kalshi-0000 operating boundary

This repository is also the local workspace used by Cursor and VS Code. When a
Codex task runs in this directory, edits are immediately visible to those
applications; no Git operation is needed merely to see local file changes.

## Live-trading safety

- Do not read, modify, stage, or commit `.env`, `logs/`, or `sessions/`. They
  contain local credentials and/or live runtime state and are intentionally
  Git-ignored.
- Treat a running bot process as independent from the source tree. Source edits
  do not change its behavior until the operator deliberately restarts it while
  flat.
- Do not pull, merge, switch branches, or restart a bot as part of an audit or
  implementation ticket unless the operator explicitly asks.

## Engineering handoffs

- Keep work bounded to the requested ticket and report changed files, test
  commands/results, and residual risk.
- For engine changes, run `py -3 -m kalshi_bot.test_engines` when practical.
- Preserve unrelated working-tree changes; do not reset or discard them.
