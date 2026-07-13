"""MeshChat Server plugin - manages a MeshChat web UI as a subprocess."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from importlib.resources import files
from typing import Any

from reticulumpi.plugin_base import PluginBase
from reticulumpi.runtime_metrics import record_process_restart


class MeshChatServer(PluginBase):
    """Starts and monitors a MeshChat web UI server over Reticulum/LXMF.

    MeshChat creates its own Reticulum instance, so both reticulumPi and MeshChat
    must connect to a shared rnsd daemon (use_shared_instance: true).
    """

    plugin_name = "meshchat_server"
    plugin_description = "Manages a MeshChat web UI server as a subprocess"
    plugin_version = "1.0.0"

    def validate_config(self) -> None:
        self._proc_lock = threading.Lock()
        install_dir = self.config.get("install_dir")
        if install_dir is None:
            # Default: <project_root>/meshchat (sibling to src/)
            install_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
                "meshchat",
            )
        install_dir = os.path.expanduser(install_dir)

        meshchat_script = os.path.join(install_dir, "meshchat.py")
        if not os.path.isfile(meshchat_script):
            raise ValueError(
                f"MeshChat not found at {meshchat_script}. "
                "Install with: git clone https://github.com/liamcottle/reticulum-meshchat "
                f"{install_dir}"
            )

        python_bin = os.path.join(install_dir, ".venv", "bin", "python")
        if not os.path.isfile(python_bin):
            raise ValueError(
                f"MeshChat venv not found at {python_bin}. "
                f"Create with: python3 -m venv {install_dir}/.venv && "
                f"{install_dir}/.venv/bin/pip install -r {install_dir}/requirements.txt"
            )

        self._install_dir = install_dir
        self._meshchat_script = meshchat_script
        self._python_bin = python_bin

        configured_storage = os.path.expanduser(
            self.config.get("storage_dir", os.path.join(self._install_dir, "storage"))
        )
        policy = getattr(getattr(self.app, "config", None), "external_artifact_policy", None)
        if getattr(policy, "required", False) is True:
            install_root = os.path.realpath(self._install_dir)
            storage_root = os.path.realpath(configured_storage)
            try:
                storage_inside_install = (
                    os.path.commonpath((install_root, storage_root)) == install_root
                )
            except ValueError:
                storage_inside_install = False
            if storage_inside_install:
                raise ValueError(
                    "production MeshChat storage must be outside the immutable install tree"
                )

        # The launcher is a first-class wheel resource.  Falling back to the
        # unpatched upstream entry point would silently drop configured safety
        # timeouts, so a broken package is fatal instead.
        launcher = files("reticulumpi").joinpath("data/meshchat_launcher.pydata")
        if not launcher.is_file():
            raise ValueError("Packaged MeshChat launcher is missing")
        self._launcher_script = str(launcher)

        link_timeout = self.config.get("link_timeout", 75)
        if not isinstance(link_timeout, (int, float)) or link_timeout <= 0:
            raise ValueError("link_timeout must be a positive number")
        self._link_timeout = link_timeout

        path_lookup_timeout = self.config.get("path_lookup_timeout", 15)
        if not isinstance(path_lookup_timeout, (int, float)) or path_lookup_timeout <= 0:
            raise ValueError("path_lookup_timeout must be a positive number")
        self._path_lookup_timeout = path_lookup_timeout

        port = self.config.get("port", 8000)
        if not isinstance(port, int) or port < 1 or port > 65535:
            raise ValueError("port must be an integer between 1 and 65535")

        host = self.config.get("host", "0.0.0.0")
        if not isinstance(host, str):
            raise ValueError("host must be a string")

        interval = self.config.get("health_check_interval", 10)
        if not isinstance(interval, (int, float)) or interval < 5:
            raise ValueError("health_check_interval must be a number >= 5")

        max_restarts = self.config.get("max_restarts", 5)
        if not isinstance(max_restarts, int) or max_restarts < 0:
            raise ValueError("max_restarts must be a non-negative integer")

    def start(self) -> None:
        self._active = True
        self._proc_lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._pid: int | None = None
        self._restart_count = 0
        self._consecutive_failures = 0
        self._last_start_monotonic: float = 0.0
        self._backoff_base = self.config.get("backoff_base_delay", 2.0)
        self._backoff_max = self.config.get("backoff_max_delay", 120.0)
        self._stability_threshold = self.config.get("stability_seconds", 60.0)

        self._host = self.config.get("host", "0.0.0.0")
        self._port = self.config.get("port", 8000)
        self._storage_dir = os.path.expanduser(
            self.config.get("storage_dir", os.path.join(self._install_dir, "storage"))
        )

        os.makedirs(self._storage_dir, exist_ok=True)

        rns_config_dir = self.app._reticulum_config_dir or os.path.expanduser("~/.reticulum")

        cmd = [
            self._python_bin,
            self._launcher_script,
            "--headless",
            "--host",
            self._host,
            "--port",
            str(self._port),
            "--storage-dir",
            self._storage_dir,
            "--reticulum-config-dir",
            rns_config_dir,
        ]
        env = os.environ.copy()
        env["MESHCHAT_DIR"] = self._install_dir
        env["MESHCHAT_LINK_TIMEOUT"] = str(int(self._link_timeout))
        env["MESHCHAT_PATH_LOOKUP_TIMEOUT"] = str(int(self._path_lookup_timeout))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        self._env = env
        self._launch_process(cmd, env=env)
        self._cmd = cmd

        self._start_thread(self._health_monitor, "meshchat-monitor")
        self.log.info(
            "MeshChat server started (PID: %d, URL: http://%s:%d)",
            self._pid,
            self._host,
            self._port,
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
            "host": getattr(self, "_host", None),
            "port": getattr(self, "_port", None),
            "web_url": f"http://{self._host}:{self._port}"
            if getattr(self, "_host", None)
            else None,
            "storage_dir": getattr(self, "_storage_dir", None),
            "restart_count": self._restart_count,
        }

    def _launch_process(self, cmd: list[str], env: dict[str, str] | None = None) -> None:
        with self._proc_lock:
            self._process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=self._storage_dir,
            )
            self._pid = self._process.pid
            self._last_start_monotonic = time.monotonic()
        self._log_thread = self._start_log_reader(self._process, prefix="meshchat")

    def _terminate_process(self) -> None:
        with self._proc_lock:
            if self._process is None:
                return
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.log.warning("MeshChat did not stop gracefully, sending SIGKILL")
                self._process.kill()
                try:
                    self._process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.log.warning("MeshChat process did not exit after SIGKILL")
            except Exception:
                self.log.exception("Error stopping MeshChat process")
            finally:
                if self._process and self._process.stdout:
                    try:
                        self._process.stdout.close()
                    except OSError:
                        pass
                self._process = None

    def _health_monitor(self) -> None:
        interval = self.config.get("health_check_interval", 10)
        max_restarts = self.config.get("max_restarts", 5)
        auto_restart = self.config.get("auto_restart", True)

        while self._active:
            self._sleep_while_active(interval)
            if not self._active:
                break

            with self._proc_lock:
                proc = self._process
                exited = proc is not None and proc.poll() is not None
                exit_code = proc.returncode if exited else None

            if exited:
                self.log.warning("MeshChat process exited unexpectedly (code: %s)", exit_code)

                # Reset failure counter if the process ran stably
                uptime = max(0.0, time.monotonic() - self._last_start_monotonic)
                if uptime >= self._stability_threshold:
                    self._consecutive_failures = 0

                if auto_restart and self._restart_count < max_restarts:
                    self._restart_count += 1
                    record_process_restart()
                    self._consecutive_failures += 1

                    # Exponential backoff between restarts
                    delay = min(
                        self._backoff_base * (2 ** (self._consecutive_failures - 1)),
                        self._backoff_max,
                    )
                    self.log.info(
                        "Restarting MeshChat in %.1fs (attempt %d/%d, consecutive failures: %d)",
                        delay,
                        self._restart_count,
                        max_restarts,
                        self._consecutive_failures,
                    )
                    self._sleep_while_active(delay)
                    if not self._active:
                        break

                    try:
                        old_log = getattr(self, "_log_thread", None)
                        if old_log is not None:
                            self._remove_thread(old_log)
                        self._launch_process(self._cmd, env=self._env)
                        self.log.info("MeshChat restarted (PID: %d)", self._pid)
                    except Exception:
                        self.log.exception("Failed to restart MeshChat")
                        self._active = False
                else:
                    self.log.error(
                        "MeshChat exceeded max restarts (%d), giving up",
                        max_restarts,
                    )
                    self._active = False
