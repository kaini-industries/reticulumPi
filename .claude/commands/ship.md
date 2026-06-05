Commit, push, and deploy all pending changes. Stop at the first failure.

1. Run `git status` and `git diff --stat` to see what's changed. If there are no uncommitted changes, say so and stop.
2. Run ruff lint on changed Python files: `.venv/bin/ruff check src/ tests/ plugins/`. If errors, show them and stop.
3. Run `git diff` to review the full diff. Draft a short, single-line commit message (under 72 characters) following the repo's style (`git log --oneline -5`). The message should focus on the "why", not the "what". Do NOT write multi-clause messages — keep it punchy.
4. Show the proposed commit message and ask the user to confirm or edit it.
5. Stage the changed files by name (not `git add -A`). Commit with the confirmed message, appending `Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>`.
6. Push to remote: `git push`. If the push fails (e.g. diverged), show the error and stop — do not force-push.
7. Deploy: `sudo bash scripts/update.sh`. If it fails, show the error and stop.
8. Verify services: `sudo systemctl status reticulumpi rnsd --no-pager -l`
9. Check logs: `sudo journalctl -u reticulumpi -n 10 --no-pager`
10. Report: files changed, commit hash, push result, service status, any ERROR lines in recent logs.
