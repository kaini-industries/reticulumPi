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
make test           # runs the test suite (~610 tests)
make lint           # runs ruff linter
make format         # auto-format code with ruff
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
6. **Run the linter**: `make lint`
7. **Commit** with a clear message describing the "why"
8. **Push** and open a pull request against `main`

## Code Style

### Python

- **Formatter/Linter**: [ruff](https://docs.astral.sh/ruff/) (configuration in `pyproject.toml`)
- **Line length**: 100 characters
- **Target**: Python 3.9+ (no walrus operators in critical paths, use `from __future__` sparingly)
- **Imports**: stdlib first, third-party second, local third (ruff enforces this)
- **Type hints**: Encouraged but not required (the codebase uses them inconsistently)
- **Docstrings**: Required for public classes and methods; Google style preferred

### Naming

- **Plugins**: `snake_case` for `plugin_name`, matching the filename
- **Events**: `UPPER_SNAKE_CASE` constants in `events.py`
- **Config keys**: `snake_case` in YAML

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
- Update the plugin count in `README.md` features list

## Writing Tests

- Test files live in `tests/` and follow the `test_<module>.py` naming convention
- Use `pytest` with `pytest-mock` for mocking
- Mock external dependencies (RNS, LXMF, hardware) -- tests must run without Reticulum
- Use the fixtures in `conftest.py` (e.g., `mock_app`, `tmp_path`)
- Aim for both happy-path and error-path coverage
- Mark integration tests that need a real Reticulum instance: `@pytest.mark.integration`

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

## Release Process

1. Update version in `pyproject.toml` and `src/reticulumpi/__init__.py`
2. Move `[Unreleased]` entries in `CHANGELOG.md` to a new version section
3. Tag the release: `git tag -a v0.2.0 -m "Release 0.2.0"`
4. Push tags: `git push --tags`

## Questions?

Open an issue or reach out to the Reticulum community. The [Reticulum documentation](https://reticulum.network/manual/) is the authoritative reference for the underlying network stack.
