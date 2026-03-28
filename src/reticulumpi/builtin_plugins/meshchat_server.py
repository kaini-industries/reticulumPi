"""MeshChat Server plugin - manages a MeshChat web UI as a subprocess."""

from __future__ import annotations

import os
import subprocess
from typing import Any

from reticulumpi.plugin_base import PluginBase


class MeshChatServer(PluginBase):
    """Starts and monitors a MeshChat web UI server over Reticulum/LXMF.

    MeshChat creates its own Reticulum instance, so both reticulumPi and MeshChat
    must connect to a shared rnsd daemon (use_shared_instance: true).
    """

    plugin_name = "meshchat_server"
    plugin_description = "Manages a MeshChat web UI server as a subprocess"
    plugin_version = "1.0.0"

    def validate_config(self) -> None:
        install_dir = self.config.get("install_dir")
        if install_dir is None:
            # Default: <project_root>/meshchat (sibling to src/)
            install_dir = os.path.join(
                os.path.dirname(
                    os.path.dirname(
                        os.path.dirname(os.path.dirname(__file__))
                    )
                ),
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
        self._process: subprocess.Popen[bytes] | None = None
        self._pid: int | None = None
        self._restart_count = 0

        self._host = self.config.get("host", "0.0.0.0")
        self._port = self.config.get("port", 8000)
        self._storage_dir = os.path.expanduser(
            self.config.get(
                "storage_dir", os.path.join(self._install_dir, "storage")
            )
        )

        os.makedirs(self._storage_dir, exist_ok=True)

        rns_config_dir = self.app._reticulum_config_dir or os.path.expanduser(
            "~/.reticulum"
        )

        cmd = [
            self._python_bin,
            self._meshchat_script,
            "--headless",
            "--host", self._host,
            "--port", str(self._port),
            "--storage-dir", self._storage_dir,
            "--reticulum-config-dir", rns_config_dir,
        ]
        self._launch_process(cmd)
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

    def _launch_process(self, cmd: list[str]) -> None:
        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        self._pid = self._process.pid
        self._start_log_reader(self._process, prefix="meshchat")

    def _terminate_process(self) -> None:
        if self._process is None:
            return
        try:
            self._process.terminate()
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.log.warning("MeshChat did not stop gracefully, sending SIGKILL")
            self._process.kill()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.log.warning("MeshChat process did not exit after SIGKILL")
        except Exception:
            self.log.exception("Error stopping MeshChat process")
        finally:
            if self._process and self._process.stdout:
                try:
                    self._process.stdout.close()
                except Exception:
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

            if self._process is not None and self._process.poll() is not None:
                exit_code = self._process.returncode
                self.log.warning(
                    "MeshChat process exited unexpectedly (code: %s)", exit_code
                )

                if auto_restart and self._restart_count < max_restarts:
                    self._restart_count += 1
                    self.log.info(
                        "Restarting MeshChat (attempt %d/%d)",
                        self._restart_count,
                        max_restarts,
                    )
                    try:
                        self._launch_process(self._cmd)
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
