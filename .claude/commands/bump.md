Bump the ReticulumPi version number. Accepts an optional version argument (e.g. `0.3.0`).

Version appears in exactly TWO canonical locations:
- `pyproject.toml` line 7: `version = "X.Y.Z"`
- `src/reticulumpi/__init__.py` line 3: `__version__ = "X.Y.Z"`

Steps:
1. Read current version from `pyproject.toml`.
2. If $ARGUMENTS is provided, use that version. Otherwise, look at `git log` since the last tag and suggest a semver bump (patch for fixes, minor for features). Ask to confirm.
3. Update both files with the new version string.
4. Add a `## [X.Y.Z] - YYYY-MM-DD` section in CHANGELOG.md above the previous release. Move [Unreleased] content into it. Add fresh empty [Unreleased].
5. Show diff of all changes.
