"""Transactional subprocess-pipeline supervision for radio decoders.

The supervisor owns one POSIX process group.  A pipeline either launches in
full or every earlier stage is terminated.  Unexpected stage exit (or an EOF
reported by a parser) restarts the whole group under a bounded monotonic-time
budget, while explicit stop is idempotent and never triggers a restart.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from reticulumpi.runtime_metrics import record_process_restart

log = logging.getLogger(__name__)

_DEFAULT_BACKOFF = (1.0, 2.0, 4.0, 8.0, 30.0)


@dataclass(frozen=True)
class ProcessSpec:
    """Immutable declaration of one stage in a managed pipeline."""

    argv: tuple[str, ...]
    name: str = ""
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    stdin: Any = None
    stdout: Any = subprocess.PIPE
    stderr: Any = subprocess.PIPE
    pipe_from_previous: bool = True
    text: bool = False
    encoding: str | None = None
    errors: str | None = None
    bufsize: int = -1

    def __post_init__(self) -> None:
        argv = tuple(self.argv)
        if not argv or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in argv):
            raise ValueError("ProcessSpec argv must contain non-empty strings without NULs")
        object.__setattr__(self, "argv", argv)
        if self.env is not None:
            environment = dict(self.env)
            if not all(
                isinstance(key, str)
                and isinstance(value, str)
                and "\0" not in key
                and "\0" not in value
                for key, value in environment.items()
            ):
                raise ValueError("ProcessSpec environment must contain valid strings")
            object.__setattr__(self, "env", MappingProxyType(environment))

    @property
    def display_name(self) -> str:
        return self.name or os.path.basename(self.argv[0]) or self.argv[0]


@dataclass(frozen=True)
class RestartPolicy:
    """Bounded restart policy evaluated with monotonic timestamps."""

    enabled: bool = True
    delays: tuple[float, ...] = _DEFAULT_BACKOFF
    max_restarts: int = 5
    window_seconds: float = 600.0
    poll_interval: float = 0.2

    def __post_init__(self) -> None:
        delays = tuple(float(delay) for delay in self.delays)
        if not delays or any(delay < 0 for delay in delays):
            raise ValueError("restart delays must be a non-empty sequence of non-negative values")
        if self.max_restarts < 0:
            raise ValueError("max_restarts cannot be negative")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        object.__setattr__(self, "delays", delays)

    def delay_for_attempt(self, attempt: int) -> float:
        if attempt < 1:
            raise ValueError("restart attempt numbers start at one")
        return self.delays[min(attempt - 1, len(self.delays) - 1)]


@dataclass(frozen=True)
class ProcessFailure:
    """Description passed to unexpected-exit hooks."""

    stage_index: int | None
    stage_name: str | None
    returncode: int | None
    reason: str
    detected_at: float


class ProcessLaunchError(RuntimeError):
    """Raised when a transactional pipeline launch fails."""


FailureHook = Callable[[ProcessFailure], None]
RestartHook = Callable[[int, float], None]
RestartFailureHook = Callable[[BaseException, int], None]
ExhaustedHook = Callable[[ProcessFailure], None]
StartedHook = Callable[[tuple[subprocess.Popen[Any], ...], bool], None]


class ManagedProcessGroup:
    """Own and supervise a complete decoder pipeline as one process group."""

    def __init__(
        self,
        specs: Sequence[ProcessSpec],
        *,
        restart_policy: RestartPolicy | None = None,
        on_unexpected_exit: FailureHook | None = None,
        on_restart: RestartHook | None = None,
        on_restart_failed: RestartFailureHook | None = None,
        on_exhausted: ExhaustedHook | None = None,
        on_started: StartedHook | None = None,
        terminate_timeout: float = 5.0,
        kill_timeout: float = 2.0,
        popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    ) -> None:
        self.specs = tuple(specs)
        if not self.specs:
            raise ValueError("ManagedProcessGroup requires at least one ProcessSpec")
        if terminate_timeout < 0 or kill_timeout < 0:
            raise ValueError("process termination timeouts cannot be negative")
        self.restart_policy = restart_policy or RestartPolicy()
        self._on_unexpected_exit = on_unexpected_exit
        self._on_restart = on_restart
        self._on_restart_failed = on_restart_failed
        self._on_exhausted = on_exhausted
        self._on_started = on_started
        self._terminate_timeout = float(terminate_timeout)
        self._kill_timeout = float(kill_timeout)
        self._popen_factory = popen_factory

        self._lock = threading.RLock()
        self._processes: list[subprocess.Popen[Any]] = []
        self._pgid: int | None = None
        self._monitor: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._failure_event = threading.Event()
        self._reported_failure: ProcessFailure | None = None
        self._restart_times: deque[float] = deque()
        self._restart_count = 0
        self._running = False
        self._starting = False
        self._launch_complete = threading.Event()
        self._launch_complete.set()
        self._started_at: float | None = None
        self._last_failure: ProcessFailure | None = None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def processes(self) -> tuple[subprocess.Popen[Any], ...]:
        with self._lock:
            return tuple(self._processes)

    @property
    def pgid(self) -> int | None:
        with self._lock:
            return self._pgid

    @property
    def restart_count(self) -> int:
        with self._lock:
            return self._restart_count

    @property
    def last_failure(self) -> ProcessFailure | None:
        with self._lock:
            return self._last_failure

    @property
    def uptime(self) -> float:
        with self._lock:
            started_at = self._started_at
            running = self._running
        if not running or started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - started_at)

    def replace_specs(self, specs: Sequence[ProcessSpec]) -> None:
        """Replace pipeline declarations between launch attempts.

        Hardware-backed plugins use this after re-resolving a device during
        the restart hook.  Replacing a live pipeline is rejected; callers must
        stop it explicitly for configuration changes.
        """

        replacement = tuple(specs)
        if not replacement:
            raise ValueError("ManagedProcessGroup requires at least one ProcessSpec")
        with self._lock:
            if self._processes:
                raise RuntimeError("cannot replace specs while processes are running")
            self.specs = replacement

    def start(self) -> tuple[subprocess.Popen[Any], ...]:
        """Launch the complete pipeline and start its daemon monitor."""

        with self._lock:
            if self._running or self._starting:
                raise RuntimeError("managed process group is already running")
            self._starting = True
            self._launch_complete.clear()
            self._stop_event.clear()
            self._failure_event.clear()
            self._reported_failure = None
            self._restart_times.clear()
            self._restart_count = 0
        try:
            processes, pgid = self._launch_transaction()
            if self._stop_event.is_set():
                self._terminate_processes(processes, pgid)
                raise ProcessLaunchError("pipeline launch cancelled")
            try:
                self._call_started_hook(processes, restarted=False)
            except BaseException as exc:
                self._terminate_processes(processes, pgid)
                raise ProcessLaunchError(f"pipeline started hook failed: {exc}") from exc

            with self._lock:
                if self._stop_event.is_set():
                    cancelled = True
                else:
                    cancelled = False
                    self._processes = processes
                    self._pgid = pgid
                    self._running = True
                    self._started_at = time.monotonic()
                    monitor = threading.Thread(
                        target=self._monitor_loop,
                        name="process-group-monitor",
                        daemon=True,
                    )
                    self._monitor = monitor
                    try:
                        monitor.start()
                    except BaseException as exc:
                        self._monitor = None
                        self._processes = []
                        self._pgid = None
                        self._running = False
                        self._started_at = None
                        monitor_error: BaseException | None = exc
                    else:
                        monitor_error = None
            if cancelled:
                self._terminate_processes(processes, pgid)
                raise ProcessLaunchError("pipeline launch cancelled")
            if monitor_error is not None:
                self._terminate_processes(processes, pgid)
                raise ProcessLaunchError(
                    f"pipeline monitor failed to start: {monitor_error}"
                ) from monitor_error
            return tuple(processes)
        finally:
            with self._lock:
                self._starting = False
                self._launch_complete.set()

    def stop(self) -> None:
        """Idempotently stop monitoring and TERM/KILL the owned process group."""

        with self._lock:
            had_work = self._running or self._starting or bool(self._processes)
            launching = self._starting
            self._running = False
            self._stop_event.set()
            self._failure_event.set()
        if not had_work:
            return
        if launching:
            self._launch_complete.wait(
                timeout=self._terminate_timeout + self._kill_timeout + 1.0,
            )
        self._terminate_current()
        with self._lock:
            monitor = self._monitor
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=self._terminate_timeout + self._kill_timeout + 1.0)
        with self._lock:
            if self._monitor is monitor:
                self._monitor = None
            self._started_at = None

    def request_stop(self) -> bool:
        """Prevent restarts and send an initial non-blocking group SIGTERM.

        This supports application-wide parallel pre-stop signalling.  A later
        :meth:`stop` call still waits within the configured shared deadline and
        escalates to SIGKILL when required.
        """

        with self._lock:
            if not self._running and not self._starting and not self._processes:
                return False
            self._running = False
            self._stop_event.set()
            self._failure_event.set()
            processes = list(self._processes)
            pgid = self._pgid
        self._signal_group(processes, pgid, signal.SIGTERM)
        return True

    def notify_unexpected_eof(
        self,
        stage_index: int | None = None,
        reason: str = "unexpected stdout EOF",
    ) -> bool:
        """Report parser EOF even if the child has not updated poll() yet."""

        with self._lock:
            if not self._running or self._stop_event.is_set():
                return False
            if stage_index is not None and not 0 <= stage_index < len(self.specs):
                raise IndexError("stage_index is outside the managed pipeline")
            spec = self.specs[stage_index] if stage_index is not None else None
            process = (
                self._processes[stage_index]
                if stage_index is not None and stage_index < len(self._processes)
                else None
            )
            self._reported_failure = ProcessFailure(
                stage_index=stage_index,
                stage_name=spec.display_name if spec else None,
                returncode=process.poll() if process is not None else None,
                reason=str(reason),
                detected_at=time.monotonic(),
            )
            self._failure_event.set()
            return True

    def _launch_transaction(self) -> tuple[list[subprocess.Popen[Any]], int]:
        processes: list[subprocess.Popen[Any]] = []
        pgid: int | None = None
        try:
            for index, spec in enumerate(self.specs):
                stdin = spec.stdin
                if index > 0 and spec.pipe_from_previous:
                    previous_stdout = processes[index - 1].stdout
                    if previous_stdout is None:
                        raise ProcessLaunchError(
                            f"pipeline stage {index - 1} has no stdout for {spec.display_name}"
                        )
                    stdin = previous_stdout
                environment = None
                if spec.env is not None:
                    environment = os.environ.copy()
                    environment.update(spec.env)
                process = self._popen_factory(
                    list(spec.argv),
                    stdin=stdin,
                    stdout=spec.stdout,
                    stderr=spec.stderr,
                    cwd=spec.cwd,
                    env=environment,
                    process_group=0 if pgid is None else pgid,
                    text=spec.text,
                    encoding=spec.encoding,
                    errors=spec.errors,
                    bufsize=spec.bufsize,
                )
                processes.append(process)
                if pgid is None:
                    pgid = process.pid
                if index > 0 and spec.pipe_from_previous:
                    previous_stdout = processes[index - 1].stdout
                    if previous_stdout is not None:
                        previous_stdout.close()
            assert pgid is not None
            return processes, pgid
        except BaseException as exc:
            self._terminate_processes(processes, pgid)
            if isinstance(exc, ProcessLaunchError):
                raise
            raise ProcessLaunchError(f"pipeline launch failed: {exc}") from exc

    def _monitor_loop(self) -> None:
        pending_failure: ProcessFailure | None = None
        while not self._stop_event.is_set():
            if pending_failure is None:
                pending_failure = self._wait_for_failure()
                if pending_failure is None:
                    continue
            with self._lock:
                self._last_failure = pending_failure
            self._terminate_current()
            # Hooks may release hardware leases. The complete process group
            # must be gone first so another owner cannot acquire a device that
            # a late pipeline stage still has open.
            self._call_failure_hook(pending_failure)
            if self._stop_event.is_set() or not self.restart_policy.enabled:
                break

            delay = self._reserve_restart()
            if delay is None:
                self._call_exhausted_hook(pending_failure)
                break
            with self._lock:
                attempt = self._restart_count
            if self._stop_event.wait(delay):
                break
            if self._on_restart is not None:
                try:
                    self._on_restart(attempt, delay)
                except BaseException as exc:
                    log.exception("Managed process restart hook failed")
                    if self._on_restart_failed is not None:
                        try:
                            self._on_restart_failed(exc, attempt)
                        except Exception:
                            log.exception("Managed process restart-failure hook failed")
                    pending_failure = ProcessFailure(
                        stage_index=None,
                        stage_name=None,
                        returncode=None,
                        reason=f"restart preparation failed: {exc}",
                        detected_at=time.monotonic(),
                    )
                    continue
            try:
                processes, pgid = self._launch_transaction()
            except BaseException as exc:
                if self._on_restart_failed is not None:
                    try:
                        self._on_restart_failed(exc, attempt)
                    except Exception:
                        log.exception("Managed process restart-failure hook failed")
                pending_failure = ProcessFailure(
                    stage_index=None,
                    stage_name=None,
                    returncode=None,
                    reason=f"restart launch failed: {exc}",
                    detected_at=time.monotonic(),
                )
                continue
            with self._lock:
                if self._stop_event.is_set():
                    discard = True
                else:
                    self._processes = processes
                    self._pgid = pgid
                    self._started_at = time.monotonic()
                    self._failure_event.clear()
                    self._reported_failure = None
                    discard = False
            if discard:
                self._terminate_processes(processes, pgid)
                break
            try:
                self._call_started_hook(processes, restarted=True)
            except BaseException as exc:
                self._terminate_current()
                if self._on_restart_failed is not None:
                    try:
                        self._on_restart_failed(exc, attempt)
                    except Exception:
                        log.exception("Managed process restart-failure hook failed")
                pending_failure = ProcessFailure(
                    stage_index=None,
                    stage_name=None,
                    returncode=None,
                    reason=f"restarted pipeline hook failed: {exc}",
                    detected_at=time.monotonic(),
                )
                continue
            pending_failure = None

        with self._lock:
            self._running = False
            self._monitor = None
            self._started_at = None

    def _wait_for_failure(self) -> ProcessFailure | None:
        while not self._stop_event.is_set():
            if self._failure_event.wait(timeout=self.restart_policy.poll_interval):
                if self._stop_event.is_set():
                    return None
                with self._lock:
                    failure = self._reported_failure
                    self._reported_failure = None
                    self._failure_event.clear()
                if failure is not None:
                    return failure
            with self._lock:
                processes = list(self._processes)
            for index, process in enumerate(processes):
                returncode = process.poll()
                if returncode is not None:
                    spec = self.specs[index]
                    return ProcessFailure(
                        stage_index=index,
                        stage_name=spec.display_name,
                        returncode=returncode,
                        reason="process exited unexpectedly",
                        detected_at=time.monotonic(),
                    )
        return None

    def _reserve_restart(self) -> float | None:
        policy = self.restart_policy
        now = time.monotonic()
        with self._lock:
            cutoff = now - policy.window_seconds
            while self._restart_times and self._restart_times[0] < cutoff:
                self._restart_times.popleft()
            if len(self._restart_times) >= policy.max_restarts:
                return None
            self._restart_times.append(now)
            self._restart_count += 1
            record_process_restart()
            attempt = len(self._restart_times)
        return policy.delay_for_attempt(attempt)

    def _call_failure_hook(self, failure: ProcessFailure) -> None:
        if self._on_unexpected_exit is None:
            return
        try:
            self._on_unexpected_exit(failure)
        except Exception:
            log.exception("Managed process failure hook failed")

    def _call_exhausted_hook(self, failure: ProcessFailure) -> None:
        if self._on_exhausted is None:
            return
        try:
            self._on_exhausted(failure)
        except Exception:
            log.exception("Managed process exhaustion hook failed")

    def _call_started_hook(
        self,
        processes: Sequence[subprocess.Popen[Any]],
        *,
        restarted: bool,
    ) -> None:
        if self._on_started is not None:
            self._on_started(tuple(processes), restarted)

    def _terminate_current(self) -> None:
        with self._lock:
            processes = self._processes
            pgid = self._pgid
            self._processes = []
            self._pgid = None
        self._terminate_processes(processes, pgid)

    def _terminate_processes(
        self,
        processes: Sequence[subprocess.Popen[Any]],
        pgid: int | None,
    ) -> None:
        if not processes:
            return
        self._signal_group(processes, pgid, signal.SIGTERM)
        survivors = self._wait_shared_deadline(processes, self._terminate_timeout)
        if survivors:
            self._signal_group(survivors, pgid, signal.SIGKILL)
            self._wait_shared_deadline(survivors, self._kill_timeout)
        self._close_streams(processes)

    @staticmethod
    def _signal_group(
        processes: Sequence[subprocess.Popen[Any]],
        pgid: int | None,
        sig: signal.Signals,
    ) -> None:
        if pgid is not None:
            try:
                os.killpg(pgid, sig)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        for process in processes:
            try:
                if process.poll() is None:
                    process.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    @staticmethod
    def _wait_shared_deadline(
        processes: Sequence[subprocess.Popen[Any]],
        timeout: float,
    ) -> list[subprocess.Popen[Any]]:
        deadline = time.monotonic() + timeout
        survivors: list[subprocess.Popen[Any]] = []
        for index, process in enumerate(processes):
            if process.poll() is not None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                survivors.append(process)
                survivors.extend(
                    candidate for candidate in processes[index + 1 :] if candidate.poll() is None
                )
                break
        return survivors

    @staticmethod
    def _close_streams(processes: Sequence[subprocess.Popen[Any]]) -> None:
        closed: set[int] = set()
        for process in processes:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is None or id(stream) in closed:
                    continue
                closed.add(id(stream))
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
