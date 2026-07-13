"""Tests for transactional decoder-process supervision."""

from __future__ import annotations

import io
import signal
import subprocess
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from reticulumpi.process_supervisor import (
    ManagedProcessGroup,
    ProcessFailure,
    ProcessLaunchError,
    ProcessSpec,
    RestartPolicy,
)
from reticulumpi.runtime_metrics import get_runtime_metrics


class FakeProcess:
    _next_pid = 9000

    def __init__(self, *, block_wait: bool = False) -> None:
        self.pid = FakeProcess._next_pid
        FakeProcess._next_pid += 1
        self.returncode: int | None = None
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.block_wait = block_wait
        self.wait_calls: list[float | None] = []
        self.signals: list[signal.Signals] = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.block_wait:
            raise subprocess.TimeoutExpired(["fake"], timeout)
        if self.returncode is None:
            self.returncode = -signal.SIGTERM
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate(), "condition did not become true"


def _policy(*, max_restarts: int = 5) -> RestartPolicy:
    return RestartPolicy(
        delays=(0, 0, 0, 0, 0),
        max_restarts=max_restarts,
        window_seconds=600,
        poll_interval=0.01,
    )


class TestDeclarations:
    def test_process_spec_normalizes_and_protects_environment(self):
        raw = {"LANG": "C"}
        spec = ProcessSpec(["decoder", "--json"], env=raw)  # type: ignore[arg-type]
        raw["LANG"] = "changed"
        assert spec.argv == ("decoder", "--json")
        assert spec.env == {"LANG": "C"}

    @pytest.mark.parametrize("argv", [(), ("",), ("bad\0arg",)])
    def test_process_spec_rejects_invalid_argv(self, argv):
        with pytest.raises(ValueError, match="argv"):
            ProcessSpec(argv)

    @pytest.mark.parametrize(
        "environment",
        [
            {"LANG": 1},
            {1: "C"},
            {"BAD\0KEY": "value"},
            {"KEY": "BAD\0VALUE"},
        ],
    )
    def test_process_spec_rejects_invalid_environment(self, environment):
        with pytest.raises(ValueError, match="environment"):
            ProcessSpec(("decoder",), env=environment)

    def test_restart_policy_uses_required_backoff(self):
        policy = RestartPolicy()
        assert [policy.delay_for_attempt(i) for i in range(1, 7)] == [
            1,
            2,
            4,
            8,
            30,
            30,
        ]

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"delays": ()}, "delays"),
            ({"delays": (-1,)}, "delays"),
            ({"max_restarts": -1}, "max_restarts"),
            ({"window_seconds": 0}, "window_seconds"),
            ({"poll_interval": 0}, "poll_interval"),
        ],
    )
    def test_restart_policy_rejects_invalid_limits(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            RestartPolicy(**kwargs)

    def test_restart_policy_rejects_nonpositive_attempt(self):
        with pytest.raises(ValueError, match="start at one"):
            RestartPolicy().delay_for_attempt(0)

    def test_group_rejects_empty_pipeline_and_negative_timeouts(self):
        with pytest.raises(ValueError, match="at least one"):
            ManagedProcessGroup([])
        with pytest.raises(ValueError, match="timeouts"):
            ManagedProcessGroup([ProcessSpec(("decoder",))], terminate_timeout=-1)
        with pytest.raises(ValueError, match="timeouts"):
            ManagedProcessGroup([ProcessSpec(("decoder",))], kill_timeout=-1)

    def test_empty_replacement_is_rejected(self):
        group = ManagedProcessGroup([ProcessSpec(("decoder",))])
        with pytest.raises(ValueError, match="at least one"):
            group.replace_specs([])

    def test_specs_can_only_be_replaced_between_launches(self):
        group = ManagedProcessGroup([ProcessSpec(("first",))])
        group.replace_specs([ProcessSpec(("second",))])
        assert group.specs[0].argv == ("second",)

        process = FakeProcess()
        group = ManagedProcessGroup(
            [ProcessSpec(("first",))],
            restart_policy=RestartPolicy(enabled=False),
            popen_factory=MagicMock(return_value=process),
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            try:
                with pytest.raises(RuntimeError, match="processes are running"):
                    group.replace_specs([ProcessSpec(("second",))])
            finally:
                group.stop()


class TestTransactionalLaunch:
    def test_runtime_metadata_and_duplicate_start(self):
        process = FakeProcess()
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=RestartPolicy(enabled=False),
            popen_factory=MagicMock(return_value=process),
        )
        assert group.pgid is None
        assert group.last_failure is None
        assert group.uptime == 0.0
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            try:
                assert group.pgid == process.pid
                assert group.uptime >= 0.0
                with pytest.raises(RuntimeError, match="already running"):
                    group.start()
            finally:
                group.stop()
        assert group.uptime == 0.0

    def test_started_hook_can_cancel_before_processes_are_published(self):
        process = FakeProcess()
        holder = {}

        def cancel(_processes, _restarted):
            assert holder["group"].request_stop() is True

        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            on_started=cancel,
            popen_factory=MagicMock(return_value=process),
        )
        holder["group"] = group
        with patch("reticulumpi.process_supervisor.os.killpg") as killpg:
            with pytest.raises(ProcessLaunchError, match="cancelled"):
                group.start()
        killpg.assert_called_once_with(process.pid, signal.SIGTERM)
        assert group.processes == ()

    def test_stop_waits_for_an_inflight_launch(self):
        entered_factory = threading.Event()
        release_factory = threading.Event()
        process = FakeProcess()

        def factory(*_args, **_kwargs):
            entered_factory.set()
            assert release_factory.wait(timeout=1.0)
            return process

        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            terminate_timeout=0.1,
            kill_timeout=0.1,
            popen_factory=factory,
        )
        launch_errors = []
        launch_thread = threading.Thread(
            target=lambda: _capture_exception(group.start, launch_errors),
        )
        stop_thread = threading.Thread(target=group.stop)
        with patch("reticulumpi.process_supervisor.os.killpg"):
            launch_thread.start()
            assert entered_factory.wait(timeout=1.0)
            stop_thread.start()
            _wait_until(group._stop_event.is_set)
            release_factory.set()
            launch_thread.join(timeout=1.0)
            stop_thread.join(timeout=1.0)

        assert not launch_thread.is_alive()
        assert not stop_thread.is_alive()
        assert len(launch_errors) == 1
        assert isinstance(launch_errors[0], ProcessLaunchError)

    def test_process_environment_is_merged_without_mutating_parent(self):
        process = FakeProcess()
        factory = MagicMock(return_value=process)
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",), env={"RETICULUMPI_TEST": "isolated"})],
            restart_policy=RestartPolicy(enabled=False),
            popen_factory=factory,
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            group.stop()
        environment = factory.call_args.kwargs["env"]
        assert environment["RETICULUMPI_TEST"] == "isolated"
        assert "PATH" in environment

    def test_pipeline_joins_one_process_group_and_connects_stdout(self):
        first = FakeProcess()
        second = FakeProcess()
        factory = MagicMock(side_effect=[first, second])
        group = ManagedProcessGroup(
            [
                ProcessSpec(("rtl_fm", "-"), name="source"),
                ProcessSpec(("decoder", "-"), name="decoder"),
            ],
            restart_policy=RestartPolicy(enabled=False),
            popen_factory=factory,
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            processes = group.start()
            try:
                assert processes == (first, second)
                assert factory.call_args_list[0].kwargs["process_group"] == 0
                assert factory.call_args_list[1].kwargs["process_group"] == first.pid
                assert factory.call_args_list[1].kwargs["stdin"] is first.stdout
                assert first.stdout.closed
            finally:
                group.stop()

    def test_started_hook_receives_initial_and_restarted_processes(self):
        first = FakeProcess()
        replacement = FakeProcess()
        starts = []
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=_policy(),
            on_started=lambda processes, restarted: starts.append((processes, restarted)),
            popen_factory=MagicMock(side_effect=[first, replacement]),
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            try:
                first.returncode = 1
                _wait_until(lambda: group.processes == (replacement,))
                assert starts == [((first,), False), ((replacement,), True)]
            finally:
                group.stop()

    def test_initial_started_hook_failure_rolls_back(self):
        process = FakeProcess()
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            on_started=lambda _processes, _restarted: (_ for _ in ()).throw(
                RuntimeError("parser setup failed")
            ),
            popen_factory=MagicMock(return_value=process),
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            with pytest.raises(ProcessLaunchError, match="parser setup failed"):
                group.start()
        assert group.processes == ()
        assert not group.running

    def test_monitor_thread_start_failure_rolls_back_pipeline(self):
        process = FakeProcess()
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            popen_factory=MagicMock(return_value=process),
        )
        with (
            patch("reticulumpi.process_supervisor.os.killpg") as killpg,
            patch.object(threading.Thread, "start", side_effect=RuntimeError("no threads")),
        ):
            with pytest.raises(ProcessLaunchError, match="monitor failed"):
                group.start()

        killpg.assert_called_once_with(process.pid, signal.SIGTERM)
        assert process.wait_calls
        assert not group.running
        assert group.processes == ()

    def test_partial_launch_failure_terminates_earlier_stages(self):
        first = FakeProcess()
        factory = MagicMock(side_effect=[first, OSError("decoder missing")])
        group = ManagedProcessGroup(
            [ProcessSpec(("source",)), ProcessSpec(("decoder",))],
            popen_factory=factory,
        )
        with patch("reticulumpi.process_supervisor.os.killpg") as killpg:
            with pytest.raises(ProcessLaunchError, match="decoder missing"):
                group.start()
        killpg.assert_called_once_with(first.pid, signal.SIGTERM)
        assert first.wait_calls
        assert first.stdout.closed and first.stderr.closed
        assert group.running is False
        assert group.processes == ()

    def test_request_stop_during_launch_cancels_and_cleans_pipeline(self):
        entered_factory = threading.Event()
        release_factory = threading.Event()
        process = FakeProcess()

        def factory(*_args, **_kwargs):
            entered_factory.set()
            assert release_factory.wait(timeout=1.0)
            return process

        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=RestartPolicy(enabled=False),
            popen_factory=factory,
        )
        errors: list[BaseException] = []

        def launch() -> None:
            try:
                group.start()
            except BaseException as exc:
                errors.append(exc)

        launch_thread = threading.Thread(target=launch)
        with patch("reticulumpi.process_supervisor.os.killpg") as killpg:
            launch_thread.start()
            assert entered_factory.wait(timeout=1.0)
            assert group.request_stop() is True
            release_factory.set()
            launch_thread.join(timeout=1.0)

        assert not launch_thread.is_alive()
        assert len(errors) == 1
        assert isinstance(errors[0], ProcessLaunchError)
        assert "cancelled" in str(errors[0])
        killpg.assert_called_once_with(process.pid, signal.SIGTERM)
        assert process.wait_calls
        assert not group.running
        assert group.processes == ()

    def test_stage_without_pipeable_stdout_rolls_back(self):
        first = FakeProcess()
        first.stdout = None
        factory = MagicMock(return_value=first)
        group = ManagedProcessGroup(
            [ProcessSpec(("source",)), ProcessSpec(("decoder",))],
            popen_factory=factory,
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            with pytest.raises(ProcessLaunchError, match="has no stdout"):
                group.start()


class TestTermination:
    def test_idle_request_stop_and_eof_report_are_noops(self):
        group = ManagedProcessGroup([ProcessSpec(("decoder",))])
        assert group.request_stop() is False
        assert group.notify_unexpected_eof() is False

    def test_invalid_eof_stage_is_rejected_while_running(self):
        process = FakeProcess()
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=RestartPolicy(enabled=False),
            popen_factory=MagicMock(return_value=process),
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            try:
                with pytest.raises(IndexError, match="outside"):
                    group.notify_unexpected_eof(1)
            finally:
                group.stop()

    def test_stop_is_idempotent(self):
        process = FakeProcess()
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=RestartPolicy(enabled=False),
            popen_factory=MagicMock(return_value=process),
        )
        with patch("reticulumpi.process_supervisor.os.killpg") as killpg:
            group.start()
            group.stop()
            group.stop()
        assert killpg.call_count == 1
        assert group.running is False
        assert group.processes == ()

    def test_request_stop_prevents_restart_before_blocking_cleanup(self):
        process = FakeProcess()
        factory = MagicMock(return_value=process)
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=_policy(),
            popen_factory=factory,
        )
        with patch("reticulumpi.process_supervisor.os.killpg") as killpg:
            group.start()
            assert group.request_stop() is True
            process.returncode = -signal.SIGTERM
            time.sleep(0.03)
            assert factory.call_count == 1
            group.stop()
        assert killpg.call_args_list[0].args == (process.pid, signal.SIGTERM)

    def test_term_timeout_escalates_to_process_group_kill(self):
        process = FakeProcess(block_wait=True)
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=RestartPolicy(enabled=False),
            terminate_timeout=0,
            kill_timeout=0,
            popen_factory=MagicMock(return_value=process),
        )
        with patch("reticulumpi.process_supervisor.os.killpg") as killpg:
            group.start()
            group.stop()
        assert [call.args[1] for call in killpg.call_args_list] == [
            signal.SIGTERM,
            signal.SIGKILL,
        ]


class TestMonitoring:
    def test_disabled_restart_policy_stops_after_first_failure(self):
        process = FakeProcess()
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",), name="disabled-restart")],
            restart_policy=RestartPolicy(enabled=False, poll_interval=0.01),
            popen_factory=MagicMock(return_value=process),
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            process.returncode = 9
            _wait_until(lambda: not group.running)
        assert group.last_failure is not None
        assert group.last_failure.stage_name == "disabled-restart"

    def test_stop_interrupts_restart_backoff(self):
        process = FakeProcess()
        factory = MagicMock(return_value=process)
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=RestartPolicy(
                delays=(10,),
                max_restarts=1,
                window_seconds=60,
                poll_interval=0.01,
            ),
            popen_factory=factory,
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            process.returncode = 1
            _wait_until(lambda: group.restart_count == 1)
            group.stop()
        assert factory.call_count == 1

    def test_stop_during_restart_launch_discards_the_replacement(self):
        first = FakeProcess()
        replacement = FakeProcess()
        holder = {}

        def factory(*_args, **_kwargs):
            if holder.get("launched"):
                assert holder["group"].request_stop() is True
                return replacement
            holder["launched"] = True
            return first

        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=_policy(max_restarts=1),
            popen_factory=factory,
        )
        holder["group"] = group
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            first.returncode = 1
            _wait_until(lambda: not group.running)
        assert replacement.wait_calls

    def test_restart_hook_and_failure_hook_exceptions_are_isolated(self):
        first = FakeProcess()
        exhausted = threading.Event()
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=_policy(max_restarts=1),
            on_restart=MagicMock(side_effect=RuntimeError("prepare failed")),
            on_restart_failed=MagicMock(side_effect=RuntimeError("report failed")),
            on_exhausted=lambda _failure: exhausted.set(),
            popen_factory=MagicMock(return_value=first),
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            first.returncode = 1
            assert exhausted.wait(2)
        assert group.restart_count == 1

    def test_restart_launch_reporter_exception_is_isolated(self):
        first = FakeProcess()
        exhausted = threading.Event()
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=_policy(max_restarts=1),
            on_restart_failed=MagicMock(side_effect=RuntimeError("report failed")),
            on_exhausted=lambda _failure: exhausted.set(),
            popen_factory=MagicMock(side_effect=[first, OSError("USB absent")]),
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            first.returncode = 1
            assert exhausted.wait(2)
        assert group.restart_count == 1

    def test_restarted_started_hook_and_reporter_exceptions_are_isolated(self):
        first = FakeProcess()
        replacement = FakeProcess()
        exhausted = threading.Event()

        def started(_processes, restarted):
            if restarted:
                raise RuntimeError("parser failed")

        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=_policy(max_restarts=1),
            on_started=started,
            on_restart_failed=MagicMock(side_effect=RuntimeError("report failed")),
            on_exhausted=lambda _failure: exhausted.set(),
            popen_factory=MagicMock(side_effect=[first, replacement]),
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            first.returncode = 1
            assert exhausted.wait(2)
        assert replacement.wait_calls

    def test_expired_restart_timestamps_leave_the_window(self):
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=RestartPolicy(delays=(0,), max_restarts=1, window_seconds=10),
        )
        group._restart_times.append(1.0)
        before = get_runtime_metrics()["process_restarts_total"]
        with patch("reticulumpi.process_supervisor.time.monotonic", return_value=20.0):
            assert group._reserve_restart() == 0.0
        assert list(group._restart_times) == [20.0]
        assert get_runtime_metrics()["process_restarts_total"] == before + 1

    def test_hook_exceptions_do_not_escape_monitor_helpers(self):
        failure = ProcessFailure(0, "decoder", 1, "failed", 1.0)
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            on_unexpected_exit=MagicMock(side_effect=RuntimeError("failure hook")),
            on_exhausted=MagicMock(side_effect=RuntimeError("exhausted hook")),
        )
        group._call_failure_hook(failure)
        group._call_exhausted_hook(failure)


class TestLowLevelCleanup:
    def test_no_hook_and_stopped_failure_wait_return_cleanly(self):
        failure = ProcessFailure(None, None, None, "stopped", 1.0)
        group = ManagedProcessGroup([ProcessSpec(("decoder",))])
        group._call_exhausted_hook(failure)
        group._stop_event.set()
        assert group._wait_for_failure() is None

    def test_signal_group_falls_back_to_individual_processes(self):
        healthy = FakeProcess()
        gone = FakeProcess()
        gone.send_signal = MagicMock(side_effect=ProcessLookupError)
        exited = FakeProcess()
        exited.returncode = 0
        with patch(
            "reticulumpi.process_supervisor.os.killpg",
            side_effect=PermissionError,
        ):
            ManagedProcessGroup._signal_group(
                [healthy, gone, exited],
                healthy.pid,
                signal.SIGTERM,
            )
        assert healthy.signals == [signal.SIGTERM]
        gone.send_signal.assert_called_once_with(signal.SIGTERM)
        assert exited.signals == []

    def test_stream_close_errors_and_duplicate_streams_are_tolerated(self):
        broken = MagicMock()
        broken.close.side_effect = ValueError("already closed")
        process = FakeProcess()
        process.stdin = broken
        process.stdout = broken
        process.stderr = None
        ManagedProcessGroup._close_streams([process])
        broken.close.assert_called_once_with()

    def test_unexpected_stage_exit_restarts_complete_pipeline(self):
        first = FakeProcess()
        replacement = FakeProcess()
        failures = []
        restarts = []
        process_sets_seen_by_failure_hook = []
        factory = MagicMock(side_effect=[first, replacement])

        def on_failure(failure):
            failures.append(failure)
            process_sets_seen_by_failure_hook.append(group.processes)

        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",), name="acars")],
            restart_policy=_policy(),
            on_unexpected_exit=on_failure,
            on_restart=lambda attempt, delay: restarts.append((attempt, delay)),
            popen_factory=factory,
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            try:
                first.returncode = 7
                _wait_until(lambda: group.processes == (replacement,))
                assert failures[0].stage_name == "acars"
                assert failures[0].returncode == 7
                assert process_sets_seen_by_failure_hook == [()]
                assert restarts == [(1, 0.0)]
                assert group.restart_count == 1
            finally:
                group.stop()

    def test_parser_can_report_unexpected_eof(self):
        first = FakeProcess()
        replacement = FakeProcess()
        failures = []
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",), name="json-decoder")],
            restart_policy=_policy(),
            on_unexpected_exit=failures.append,
            popen_factory=MagicMock(side_effect=[first, replacement]),
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            try:
                assert group.notify_unexpected_eof(0, "JSON stream ended") is True
                _wait_until(lambda: group.processes == (replacement,))
                assert failures[0].reason == "JSON stream ended"
            finally:
                group.stop()

    def test_restart_launch_failure_is_reported_then_retried(self):
        first = FakeProcess()
        replacement = FakeProcess()
        restart_failures = []
        factory = MagicMock(side_effect=[first, OSError("USB gone"), replacement])
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=_policy(),
            on_restart_failed=lambda error, attempt: restart_failures.append((str(error), attempt)),
            popen_factory=factory,
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            try:
                first.returncode = 1
                _wait_until(lambda: group.processes == (replacement,))
                assert "USB gone" in restart_failures[0][0]
                assert restart_failures[0][1] == 1
                assert group.restart_count == 2
            finally:
                group.stop()

    def test_restart_preparation_failure_consumes_budget_without_stale_launch(self):
        first = FakeProcess()
        replacement = FakeProcess()
        preparations = MagicMock(side_effect=[RuntimeError("USB absent"), None])
        restart_failures = []
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=_policy(),
            on_restart=preparations,
            on_restart_failed=lambda error, attempt: restart_failures.append((str(error), attempt)),
            popen_factory=MagicMock(side_effect=[first, replacement]),
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            try:
                first.returncode = 1
                _wait_until(lambda: group.processes == (replacement,))
                assert restart_failures == [("USB absent", 1)]
                assert group.restart_count == 2
            finally:
                group.stop()

    def test_restart_budget_exhaustion_stops_supervisor(self):
        processes = [FakeProcess() for _ in range(3)]
        exhausted = threading.Event()
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=_policy(max_restarts=2),
            on_exhausted=lambda _failure: exhausted.set(),
            popen_factory=MagicMock(side_effect=processes),
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            processes[0].returncode = 1
            _wait_until(lambda: group.processes == (processes[1],))
            processes[1].returncode = 1
            _wait_until(lambda: group.processes == (processes[2],))
            processes[2].returncode = 1
            assert exhausted.wait(2)
            _wait_until(lambda: not group.running)
        assert group.restart_count == 2
        assert group.processes == ()

    def test_explicit_stop_does_not_emit_failure_or_restart(self):
        process = FakeProcess()
        failures = []
        factory = MagicMock(return_value=process)
        group = ManagedProcessGroup(
            [ProcessSpec(("decoder",))],
            restart_policy=_policy(),
            on_unexpected_exit=failures.append,
            popen_factory=factory,
        )
        with patch("reticulumpi.process_supervisor.os.killpg"):
            group.start()
            group.stop()
        assert failures == []
        assert factory.call_count == 1


def _capture_exception(callback, errors: list[BaseException]) -> None:
    try:
        callback()
    except BaseException as exc:
        errors.append(exc)
