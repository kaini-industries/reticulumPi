"""NomadNet Server plugin - manages a NomadNet page server as a subprocess."""

import glob
import os
import shutil
import subprocess
from typing import Any

from reticulumpi.plugin_base import PluginBase


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
            raise ValueError(
                "NomadNet binary not found. Install it with: pip install nomadnet"
            )
        self._nomadnet_bin = nomadnet_bin

        interval = self.config.get("health_check_interval", 30)
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
        interval = self.config.get("health_check_interval", 30)
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
