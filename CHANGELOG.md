# Changelog

All notable changes to ReticulumPi will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.1] - 2026-03-26

### Fixed
- Reticulum config examples now use correct format (`[interfaces]` section + `[[double brackets]]` for interface definitions)
- Bootstrap script creates all directories required by systemd `ReadWritePaths` (fixes exit code 226 on first start)
- NomadNet server plugin falls back to checking the running venv when `shutil.which("nomadnet")` fails under systemd
- Systemd service sets `PATH` to include the venv bin directory
- TCP Client Interface example now points to a real community hub instead of `example.com`

### Changed
- Bootstrap `--with-nomadnet` now auto-configures `use_shared_instance: true` and enables the `nomadnet_server` plugin
- Update script (`update.sh`) now syncs changed systemd service files and runs `daemon-reload`

## [0.1.0] - 2025-01-01

### Added
- Plugin-based architecture with abstract `PluginBase` class
- Three built-in plugins: heartbeat announce, LXMF message echo, system monitor
- Persistent cryptographic identity management
- YAML configuration with validation and useful error messages
- CLI with `--version`, `--config`, `--reticulum-config`, and `--log-level` flags
- Bootstrap script for automated Raspberry Pi deployment
- Update script for pulling latest code and upgrading dependencies
- Systemd service with security hardening
- Docker support with health check
- Comprehensive Reticulum config example covering all 12 interface types
- Connectivity guide in README covering LoRa, serial, packet radio, I2P, and more
- `make format` and `make test-cov` targets
- MIT LICENSE file
