Comprehensive project health analysis. Run all 7 sections, parallelizing independent checks within each. Grade each section (HEALTHY / DEGRADED / CRITICAL) and compute an overall grade.

## 1. Test Suite

1. **Tests pass:** `.venv/bin/pytest tests/ -v --tb=short -q 2>&1 | tail -30`
   - Any failure = CRITICAL.
2. **Coverage:** `.venv/bin/pytest tests/ --cov=reticulumpi --cov-report=term-missing -q 2>&1 | grep TOTAL`
   - <70% = DEGRADED, <50% = CRITICAL.
3. **Test count drift:** `ls tests/test_*.py | wc -l` -- compare to count in root CLAUDE.md.
   - Mismatch = INFO (flag for update).

## 2. Lint & Format

4. **Ruff lint:** `.venv/bin/ruff check src/ plugins/ tests/ --statistics 2>&1`
   - Any violations = DEGRADED.
5. **Ruff format:** `.venv/bin/ruff format --check src/ plugins/ tests/ 2>&1`
   - Any drift = DEGRADED.

## 3. Test Coverage Gaps

Static analysis only -- no test execution needed. Run in parallel with section 2.

6. **Untested plugins:** List plugin .py files in `src/reticulumpi/builtin_plugins/` (excluding `__init__.py`, `__pycache__`, `signal_plugin_base.py`, `example_plugin.py`, `lora_analysis.py`, `lora_decode.py`, `web_dashboard.py`). For each, check if a corresponding `tests/test_<name>.py` exists. List the gaps.
   - >3 gaps = DEGRADED, >6 = CRITICAL.
7. **Untested core modules:** Same pattern for `src/reticulumpi/*.py` (excluding `__init__.py`, `_paths.py`, `events.py`).
   - Any gap = DEGRADED.
8. **Frontend tests:** `find src/ -name '*.test.js' -o -name '*.spec.js' | wc -l`
   - 0 = INFO (known gap, note it).

## 4. Dependency Health

9. **Outdated packages:** `/opt/reticulumpi/.venv/bin/pip list --outdated --format=columns 2>/dev/null`
    - Count packages. >10 outdated = DEGRADED.
10. **Core dep versions:** `/opt/reticulumpi/.venv/bin/pip show rns lxmf aiohttp psutil 2>/dev/null | grep -E "^(Name|Version)"`
    - Report current versions.
11. **Security audit:** `/opt/reticulumpi/.venv/bin/pip-audit --format=columns 2>/dev/null || echo "pip-audit not installed -- skip"`
    - Any known vulnerability = CRITICAL. Tool missing = INFO.

## 5. Git & Repo Hygiene

12. **Uncommitted changes:** `git status --porcelain`
    - Any changes = INFO.
13. **Version sync:** Extract version from `pyproject.toml` (line ~7, `version = "..."`) and `src/reticulumpi/__init__.py` (line ~3, `__version__ = "..."`). They must match.
    - Mismatch = CRITICAL.
14. **Commit cadence:** `git log --oneline --since="30 days ago" | wc -l`
    - 0 in 30 days = INFO.
15. **Unpushed commits:** `git log --oneline origin/main..HEAD 2>/dev/null | wc -l`
    - Any unpushed = INFO.

## 6. Runtime Health

Quick subset -- defer to `/status` for full node diagnostics.

16. **Services:** `sudo systemctl is-active reticulumpi rnsd`
    - Either not active = CRITICAL.
17. **Recent errors:** `sudo journalctl -u reticulumpi --since "24 hours ago" -p err --no-pager -q | wc -l`
    - >10 = DEGRADED, >50 = CRITICAL.
18. **Disk space:** `df -h / | awk 'NR==2{print $5}'` (extract usage %).
    - >85% = DEGRADED, >95% = CRITICAL.

## 7. Architecture & Scale

Informational only -- no grading, just report the numbers.

19. **Plugin count:** `ls src/reticulumpi/builtin_plugins/*.py | grep -v __init__ | grep -v __pycache__ | wc -l`
20. **Event count:** `grep -c "^[A-Z_]* = " src/reticulumpi/events.py`
21. **Code scale:** `find src/ -name '*.py' | xargs wc -l | tail -1` and `find tests/ -name '*.py' | xargs wc -l | tail -1`
22. **JS files:** `find src/ -name '*.js' -not -path '*/vendor/*' | wc -l`

## Output

Present results as:

1. **Overall grade** at the top: CRITICAL if any section critical, DEGRADED if any degraded, HEALTHY otherwise.
2. **Summary table** with one row per section: Section | Grade | Key Finding.
3. **Details** per section -- only expand sections that are DEGRADED or CRITICAL. For each, list specific findings and a suggested action.
4. **Architecture & Scale** section is always shown as an info block.

If a section is HEALTHY, one line in the summary table is enough -- no detail expansion needed.
