# Contributing to ReticulumPi

Thanks for your interest in contributing! ReticulumPi is an open-source project and welcomes contributions from everyone.

## Code of Conduct

Be respectful, constructive, and patient. The Reticulum community values autonomy, privacy, and decentralization -- keep these principles in mind when proposing changes.

## Getting Started

### Development Setup

```bash
git clone https://github.com/kaini-industries/reticulumPi.git
cd reticulumPi
make dev            # creates venv + installs in editable mode with dev deps
make test           # parallel full suite
make test-serial    # serial coverage/debug lane
make lint           # runs ruff linter
make format-check   # verifies committed formatting
make format         # auto-format code with ruff
make package-check  # verifies wheel metadata and packaged assets
make docs-check     # links, generated references, stale references, ledger, CLI help
```

### Running Locally

```bash
.venv/bin/reticulumpi --config config/reticulumpi/config.example.yaml
```

### Validating Without Starting

```bash
.venv/bin/reticulumpi --check --config config/reticulumpi/config.example.yaml
```

## How to Contribute

### Reporting Bugs

Open a GitHub issue with:
- What you expected to happen
- What actually happened
- Steps to reproduce
- Your environment (OS, Python version, Reticulum version)
- Relevant log output (`journalctl -u reticulumpi --no-pager -n 100`)

### Suggesting Features

Open a GitHub issue tagged `[Feature Request]` with:
- The problem you're trying to solve
- Your proposed solution
- Alternatives you've considered

### Submitting Code

1. **Fork** the repository
2. **Create a branch** from `main`: `git checkout -b my-feature`
3. **Make your changes** following the code style below
4. **Add tests** for any new functionality
5. **Run the full test suite**: `make test`
6. **Run static/package checks**: `make lint format-check package-check`
7. **Commit** with a clear message describing the "why"
8. **Push** and open a pull request against `main`

## Code Style

### Python

- **Formatter/Linter**: [ruff](https://docs.astral.sh/ruff/) (configuration in `pyproject.toml`)
- **Line length**: 100 characters
- **Target**: Python 3.11+; CI also covers Python 3.12, 3.13, and 3.14
- **Imports**: stdlib first, third-party second, local third (ruff enforces this)
- **Type hints**: Encouraged but not required (the codebase uses them inconsistently)
- **Docstrings**: Required for public classes and methods; Google style preferred

### Naming

- **Plugins**: `snake_case` for `plugin_name`, matching the filename
- **Events**: `UPPER_SNAKE_CASE` constants in `events.py`
- **Config keys**: `snake_case` in YAML

### Dashboard assets

Node.js is a development-only dependency. After changing dashboard JavaScript or CSS, run
`npm ci`, `npm run build:dashboard`, and `npm run check:dashboard`, then commit the generated
manifest and content-addressed files. Production wheels do not install Node.js. See the
[dashboard asset pipeline](docs/dashboard-assets.md) for bundle boundaries and verification.

### Commit Messages

- Use present tense ("Add feature" not "Added feature")
- First line: short summary (under 72 characters)
- Body: explain *why*, not *what* (the diff shows the what)

## Project Structure

See the [Project Structure section in README](README.md#project-structure) for the full file layout.

Key directories for contributors:

| Directory | Purpose |
|-----------|---------|
| `src/reticulumpi/` | Core application code |
| `src/reticulumpi/builtin_plugins/` | Built-in plugins (one `.py` per plugin) |
| `tests/` | pytest test suite |
| `config/` | Example configuration files |
| `docs/` | Documentation |
| `scripts/` | Deployment and utility scripts |

## Writing Plugins

If you're building a plugin (either for yourself or to contribute upstream), see the [Plugin Development Guide](docs/plugin-development.md) for the complete walkthrough.

**Contributing a plugin upstream:**
- Place it in `src/reticulumpi/builtin_plugins/`
- Add configuration documentation to `config/reticulumpi/config.example.yaml`
- Add a section to `docs/plugins.md`
- Write tests in `tests/test_<plugin_name>.py`
- Regenerate and review the code-derived reference with `make docs-reference-refresh`

## Writing Tests

- Test files live in `tests/` and follow the `test_<module>.py` naming convention
- Use `pytest` with `pytest-mock` for mocking
- Mock external dependencies (RNS, LXMF, hardware) -- tests must run without Reticulum
- Use the fixtures in `conftest.py` (e.g., `mock_app`, `tmp_path`)
- Aim for both happy-path and error-path coverage
- Mark integration tests that need a real Reticulum instance: `@pytest.mark.integration`
- Pytest treats warnings as errors in local and CI lanes. Fix or narrowly justify the source;
  do not add broad warning suppressions.

Run tests:

```bash
make test                    # full suite
make test-cov                # with coverage report
python -m pytest tests/test_my_plugin.py -v   # single file
```

## Documentation

- **README.md** -- Gateway document with quick start and overview (keep it concise)
- **docs/** -- Detailed reference docs (API, plugins, troubleshooting, etc.)
- **config.example.yaml** -- Inline documentation for all config options
- **Code comments** -- Explain *why*, not *what*

When making changes:
- Update `docs/` if you add/change API endpoints, plugin behavior, or configuration
- Update `config.example.yaml` if you add new config options
- Update `CHANGELOG.md` under an `[Unreleased]` section

Run the dependency-light documentation gate after changing docs, configuration examples, or
CLI arguments:

```bash
python tools/check_docs.py
```

The gate validates local Markdown links, rejects legacy normative paths/security claims,
unsupported Python versions, and hand-maintained code counts, and compares both top-level
CLI help screens with committed snapshots under `docs/cli-help/`. It also regenerates in
memory and compares the plugin, event, route, and core-default inventory, and requires the
fixed 52-finding audit ledger ID set exactly once. Historical/non-normative documents are
excluded only from the stale-reference scan; their local links are still checked.

After intentionally changing a route registration, core default, plugin metadata, or event
constant, regenerate and review the code-derived snapshot:

```bash
make docs-reference-refresh
git diff -- docs/generated-code-reference.md
make docs-check
```

For an intentional CLI change, review the new help first, then refresh and commit both
snapshots:

```bash
python tools/check_docs.py --refresh-help
git diff -- docs/cli-help/
python tools/check_docs.py
```

Do not refresh snapshots merely to make CI green; the help output is part of the public
operator interface.

## Release Process

1. Complete the release-verification record and remediation matrix.
2. Move `[Unreleased]` entries in `CHANGELOG.md` to a versioned section.
3. Freeze the release commit; create and locally verify a signed annotated
   `vMAJOR.MINOR.PATCH` tag. Never edit a version string in package files.
4. Push the immutable tag so CI builds the wheel, sdist, containers, checksums, SBOM, and
   provenance once using the committed hash-locked dependency profiles.
5. Promote those exact artifacts after hardware qualification; do not rebuild or move the tag.

See [docs/release-process.md](docs/release-process.md) for the complete gate.

## Questions?

Open an issue or reach out to the Reticulum community. The [Reticulum documentation](https://reticulum.network/manual/) is the authoritative reference for the underlying network stack.
