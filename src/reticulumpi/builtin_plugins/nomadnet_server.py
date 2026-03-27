"""NomadNet Server plugin - manages a NomadNet page server as a subprocess."""

import glob
import os
import shutil
import subprocess
from typing import Any

from reticulumpi.plugin_base import PluginBase

# Minimal NomadNet config with node hosting enabled.
# Written before first launch so NomadNet starts serving pages immediately
# without the launch-patch-restart cycle.
_DEFAULT_NOMADNET_CONFIG = """\
[logging]
loglevel = 4
destination = file

[client]
enable_client = yes
user_interface = text
announce_at_start = yes
try_propagation_on_send_fail = yes

[textui]
intro_time = 1
theme = dark

[node]
enable_node = yes
node_name = {node_name}
announce_at_start = yes
disable_propagation = yes
"""


class NomadNetServer(PluginBase):
    """Starts and monitors a NomadNet daemon for serving pages over Reticulum.

    NomadNet creates its own Reticulum instance, so both reticulumPi and NomadNet
    must connect to a shared rnsd daemon (use_shared_instance: true).
    """

    plugin_name = "nomadnet_server"
    plugin_description = "Manages a NomadNet page server as a subprocess"
    plugin_version = "1.0.0"

    def validate_config(self) -> None:
        nomadnet_bin = shutil.which("nomadnet")
        if nomadnet_bin is None:
            # Fall back to checking the same venv that reticulumpi is running from.
            # This handles systemd environments where the venv bin isn't in PATH.
            import sys

            venv_bin = os.path.join(os.path.dirname(sys.executable), "nomadnet")
            if os.path.isfile(venv_bin) and os.access(venv_bin, os.X_OK):
                nomadnet_bin = venv_bin
            else:
                raise ValueError(
                    "NomadNet binary not found. Install it with: pip install nomadnet"
                )
        self._nomadnet_bin = nomadnet_bin

        interval = self.config.get("health_check_interval", 10)
        if not isinstance(interval, (int, float)) or interval < 5:
            raise ValueError("health_check_interval must be a number >= 5")

        max_restarts = self.config.get("max_restarts", 5)
        if not isinstance(max_restarts, int) or max_restarts < 0:
            raise ValueError("max_restarts must be a non-negative integer")

    def start(self) -> None:
        self._active = True
        self._process: subprocess.Popen[bytes] | None = None
        self._pid: int | None = None
        self._restart_count = 0

        self._config_dir = os.path.expanduser(
            self.config.get("config_dir", "~/.nomadnet")
        )
        self._pages_dir = os.path.join(self._config_dir, "storage", "pages")
        self._files_dir = os.path.join(self._config_dir, "storage", "files")

        self._ensure_directories()
        self._write_default_config()
        self._install_example_pages()

        rns_config_dir = self.app._reticulum_config_dir or os.path.expanduser(
            "~/.reticulum"
        )

        cmd = [
            self._nomadnet_bin,
            "--daemon",
            "--config", self._config_dir,
            "--rnsconfig", rns_config_dir,
        ]
        self._launch_process(cmd)
        self._cmd = cmd

        self._start_thread(self._health_monitor, "nomadnet-monitor")
        self.log.info(
            "NomadNet server started (PID: %d, config: %s)",
            self._pid,
            self._config_dir,
        )

    def stop(self) -> None:
        self._active = False
        self._terminate_process()
        self._join_threads()

    def get_status(self) -> dict[str, Any]:
        running = self._process is not None and self._process.poll() is None
        return {
            "active": self._active,
            "pid": self._pid,
            "running": running,
            "config_dir": getattr(self, "_config_dir", None),
            "restart_count": self._restart_count,
        }

    def _launch_process(self, cmd: list[str]) -> None:
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._pid = self._process.pid

    def _terminate_process(self) -> None:
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.log.warning("NomadNet did not stop gracefully, sending SIGKILL")
            self._process.kill()
            self._process.wait(timeout=5)
        except Exception:
            self.log.exception("Error stopping NomadNet process")

    def _health_monitor(self) -> None:
        interval = self.config.get("health_check_interval", 10)
        max_restarts = self.config.get("max_restarts", 5)
        auto_restart = self.config.get("auto_restart", True)

        while self._active:
            self._sleep_while_active(interval)
            if not self._active:
                break

            if self._process is not None and self._process.poll() is not None:
                exit_code = self._process.returncode
                self.log.warning(
                    "NomadNet process exited unexpectedly (code: %s)", exit_code
                )

                if auto_restart and self._restart_count < max_restarts:
                    self._restart_count += 1
                    self.log.info(
                        "Restarting NomadNet (attempt %d/%d)",
                        self._restart_count,
                        max_restarts,
                    )
                    try:
                        self._launch_process(self._cmd)
                        self.log.info("NomadNet restarted (PID: %d)", self._pid)
                    except Exception:
                        self.log.exception("Failed to restart NomadNet")
                        self._active = False
                else:
                    self.log.error(
                        "NomadNet exceeded max restarts (%d), giving up", max_restarts
                    )
                    self._active = False

    def _ensure_directories(self) -> None:
        for d in (self._config_dir, self._pages_dir, self._files_dir):
            os.makedirs(d, exist_ok=True)

    def _write_default_config(self) -> None:
        """Write a default NomadNet config with node hosting enabled.

        Only writes if no config file exists yet. This avoids the old
        launch-wait-patch-restart cycle: NomadNet starts correctly on the
        very first launch with node hosting already enabled.
        """
        config_file = os.path.join(self._config_dir, "config")
        if os.path.isfile(config_file):
            return

        node_name = self.config.get("node_name", "ReticulumPi")
        content = _DEFAULT_NOMADNET_CONFIG.format(node_name=node_name)

        try:
            with open(config_file, "w") as f:
                f.write(content)
            self.log.info(
                "Created NomadNet config with node hosting enabled (node_name: %s)",
                node_name,
            )
        except OSError:
            self.log.exception("Failed to write default NomadNet config")

    def _install_example_pages(self) -> None:
        existing = glob.glob(os.path.join(self._pages_dir, "*.mu"))
        if existing:
            return

        example_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
            "config",
            "nomadnet",
            "pages",
        )
        if not os.path.isdir(example_dir):
            return

        for mu_file in glob.glob(os.path.join(example_dir, "*.mu")):
            dest = os.path.join(self._pages_dir, os.path.basename(mu_file))
            shutil.copy2(mu_file, dest)
            self.log.info("Installed example page: %s", os.path.basename(mu_file))
