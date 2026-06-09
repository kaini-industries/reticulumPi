Commit, push, and deploy all pending changes. Stop at the first failure.

1. Run `git status` and `git diff --stat` to see what's changed. If there are no uncommitted changes, say so and stop.
2. Run ruff lint on changed Python files: `.venv/bin/ruff check src/ tests/ plugins/`. If errors, show them and stop.
3. Run `git diff` to review the full diff. Draft exactly ONE short line as the commit message (under 72 characters). Follow the repo's style (`git log --oneline -5`). Focus on "why", not "what". No semicolons, no "and" joining multiple thoughts, no body paragraphs — one punchy line only.
4. Show the proposed commit message and ask the user to confirm or edit it.
5. Stage the changed files by name (not `git add -A`). Commit with the confirmed message, appending `Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>` as a trailer via HEREDOC.
6. Run tests locally: `.venv/bin/pytest tests/ -v -n auto --timeout=60`. If any tests fail, show the failures and stop — do not push broken code.
7. Push to remote: `git push`. If the push fails (e.g. diverged), show the error and stop — do not force-push.
8. Deploy: `sudo bash scripts/update.sh`. If it fails, show the error and stop.
9. Verify services: `sudo systemctl status reticulumpi rnsd --no-pager -l`
10. Check logs: `sudo journalctl -u reticulumpi -n 10 --no-pager`
11. Check CI: run `gh run list --branch main -L 1 --json status,conclusion,name,databaseId` to get the latest workflow run. If it's completed, report pass/fail. If still in progress, tell the user to check with `gh run watch`.
12. Report: files changed, commit hash, push result, service status, CI status, any ERROR lines in recent logs.
