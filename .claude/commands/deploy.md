Run the full deploy cycle for ReticulumPi. Stop at the first failure.

1. Run ruff lint: `.venv/bin/ruff check src/ tests/ plugins/`. If errors, show them and stop.
2. Run test suite: `.venv/bin/pytest tests/ -x -q --tb=short`. If failures, show them and stop.
3. Deploy: `sudo bash scripts/update.sh`
4. Verify services: `sudo systemctl status reticulumpi rnsd --no-pager -l`
5. Check logs: `sudo journalctl -u reticulumpi -n 20 --no-pager`
6. Report: lint status, test count/results, service status, any ERROR lines in recent logs.
