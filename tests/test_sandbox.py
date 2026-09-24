"""Sandbox behaviour: disposable workspaces, environment scrubbing, limits.

The Docker-specific tests are skipped when no daemon is reachable; the controls
they assert on are also asserted statically (on the argument list the backend
builds) so the security posture is still covered on a machine without Docker.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import requires_docker
from patchpilot_core.enums import CommandKind, SandboxStatus
from patchpilot_core.errors import SandboxError
from patchpilot_core.models import CommandSpec, SandboxLimits
from patchpilot_sandbox import (
    BASE_ENV,
    DockerSandbox,
    LocalSubprocessSandbox,
    build_env,
    copy_snapshot,
    disposable_workspace,
    dispose,
    inspect,
    select_sandbox,
)
from patchpilot_sandbox.workspace import COPY_EXCLUDES


class TestWorkspace:
    def test_copies_the_repository_without_vcs_or_caches(
        self, fixture_repo: Path, tmp_path: Path
    ) -> None:
        (fixture_repo / ".git").mkdir()
        (fixture_repo / ".git" / "config").write_text("[core]\n", encoding="utf-8")
        (fixture_repo / "__pycache__").mkdir()
        (fixture_repo / "__pycache__" / "x.pyc").write_bytes(b"\x00")

        workspace = tmp_path / "ws"
        info = copy_snapshot(fixture_repo, workspace)

        assert (workspace / "calc_service" / "operations.py").is_file()
        assert not (workspace / ".git").exists()
        assert not (workspace / "__pycache__").exists()
        assert info.file_count > 0
        assert len(info.digest) == 32

    def test_the_digest_tracks_content(self, fixture_repo: Path, tmp_path: Path) -> None:
        first = copy_snapshot(fixture_repo, tmp_path / "a").digest
        again = copy_snapshot(fixture_repo, tmp_path / "b").digest
        assert first == again

        (fixture_repo / "calc_service" / "operations.py").write_text("changed\n", encoding="utf-8")
        assert copy_snapshot(fixture_repo, tmp_path / "c").digest != first

    def test_copying_over_an_existing_workspace_replaces_it(
        self, fixture_repo: Path, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        (workspace / "stale.txt").write_text("old", encoding="utf-8")
        copy_snapshot(fixture_repo, workspace)
        assert not (workspace / "stale.txt").exists()

    def test_each_attempt_gets_an_independent_copy(
        self, fixture_repo: Path, tmp_path: Path
    ) -> None:
        first = tmp_path / "attempt-1"
        second = tmp_path / "attempt-2"
        copy_snapshot(fixture_repo, first)
        (first / "calc_service" / "operations.py").write_text("patched\n", encoding="utf-8")
        copy_snapshot(fixture_repo, second)
        assert "patched" not in (second / "calc_service" / "operations.py").read_text(
            encoding="utf-8"
        )

    def test_disposal_removes_everything(self, fixture_repo: Path, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        copy_snapshot(fixture_repo, workspace)
        dispose(workspace)
        assert not workspace.exists()
        dispose(workspace)  # idempotent

    def test_context_manager_cleans_up_even_on_error(
        self, fixture_repo: Path, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        with pytest.raises(RuntimeError), disposable_workspace(fixture_repo, workspace):
            raise RuntimeError("boom")
        assert not workspace.exists()

    def test_refuses_an_oversized_repository(
        self, fixture_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("patchpilot_sandbox.workspace.MAX_WORKSPACE_FILES", 1)
        with pytest.raises(SandboxError, match="too large"):
            copy_snapshot(fixture_repo, tmp_path / "ws")

    def test_excludes_cover_the_dangerous_directories(self) -> None:
        assert {".git", ".venv", "node_modules"} <= COPY_EXCLUDES

    def test_inspect_reports_counts(self, fixture_repo: Path) -> None:
        info = inspect(fixture_repo)
        assert info.file_count >= 4
        assert info.total_bytes > 0


class TestEnvironmentScrubbing:
    def test_the_base_environment_carries_no_host_state(self) -> None:
        env = build_env()
        assert env == BASE_ENV
        assert "OPENAI_API_KEY" not in env
        assert "PATCHPILOT_DATABASE_URL" not in env

    @pytest.mark.parametrize(
        "name",
        [
            "GITHUB_TOKEN",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AWS_SECRET_ACCESS_KEY",
            "MY_APP_PASSWORD",
            "SSH_AUTH_SOCK",
            "DOCKER_HOST",
            "SOME_CREDENTIAL",
        ],
    )
    def test_credential_shaped_variables_are_refused(self, name: str) -> None:
        assert name not in build_env({name: "leaked"})

    def test_benign_extras_are_allowed_through(self) -> None:
        assert build_env({"PYTEST_ADDOPTS": "-x"})["PYTEST_ADDOPTS"] == "-x"

    def test_output_is_capped_and_secrets_are_redacted(self) -> None:
        from patchpilot_sandbox import capture

        limits = SandboxLimits(max_output_bytes=2_000)
        captured = capture("x" * 50_000, "token sk-abcdefghijklmnopqrstuvwxyz", limits)
        assert captured.truncated
        assert captured.dropped > 0
        assert len(captured.stdout) <= 2_000
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in captured.stderr
        assert "[redacted:openai-key]" in captured.stderr


class TestLocalSandbox:
    def test_runs_a_command_and_captures_the_exit_code(self, tmp_path: Path) -> None:
        sandbox = LocalSubprocessSandbox()
        execution = sandbox.run(
            tmp_path,
            [CommandSpec(kind=CommandKind.TEST, command="python -c \"print('hello')\"")],
            limits=SandboxLimits(timeout_seconds=60),
        )
        assert execution.status is SandboxStatus.COMPLETED
        result = execution.results[0]
        assert result.exit_code == 0
        assert "hello" in result.stdout

    def test_reports_a_non_zero_exit_without_calling_it_a_sandbox_failure(
        self, tmp_path: Path
    ) -> None:
        execution = LocalSubprocessSandbox().run(
            tmp_path,
            [CommandSpec(kind=CommandKind.TEST, command='python -c "raise SystemExit(3)"')],
            limits=SandboxLimits(timeout_seconds=60),
        )
        assert execution.status is SandboxStatus.COMPLETED
        assert execution.results[0].exit_code == 3
        assert not execution.results[0].passed

    def test_enforces_the_wall_clock_timeout(self, tmp_path: Path) -> None:
        execution = LocalSubprocessSandbox().run(
            tmp_path,
            [
                CommandSpec(
                    kind=CommandKind.TEST,
                    command='python -c "import time; time.sleep(30)"',
                    timeout_seconds=2,
                )
            ],
            limits=SandboxLimits(timeout_seconds=2),
        )
        assert execution.status is SandboxStatus.TIMEOUT
        assert execution.results[0].exit_code is None

    def test_a_timeout_kills_the_whole_process_tree(self, tmp_path: Path) -> None:
        """A grandchild must not outlive the timeout.

        The command starts a second interpreter that would write a marker file
        after three seconds. If only the direct child (the shell, or the venv
        launcher on Windows) were killed, the grandchild would keep running,
        write the marker, and hold the output pipes open until it finished.
        """
        (tmp_path / "linger.py").write_text(
            "import subprocess, sys\n"
            "subprocess.run([sys.executable, '-c', "
            '\'import pathlib, time; time.sleep(3); pathlib.Path("survived").write_text("x")\'])\n',
            encoding="utf-8",
        )
        started = time.monotonic()
        execution = LocalSubprocessSandbox().run(
            tmp_path,
            [CommandSpec(kind=CommandKind.TEST, command="python linger.py", timeout_seconds=1)],
            limits=SandboxLimits(timeout_seconds=1),
        )
        elapsed = time.monotonic() - started

        assert execution.status is SandboxStatus.TIMEOUT
        assert "was killed" in execution.results[0].stderr
        assert elapsed < 3, "the kill must not wait for the grandchild to finish"
        time.sleep(max(0.0, 4.5 - elapsed))
        assert not (tmp_path / "survived").exists()

    def test_output_that_is_not_valid_utf8_is_captured_not_fatal(self, tmp_path: Path) -> None:
        execution = LocalSubprocessSandbox().run(
            tmp_path,
            [
                CommandSpec(
                    kind=CommandKind.TEST,
                    command="python -c \"import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe')\"",
                )
            ],
            limits=SandboxLimits(timeout_seconds=60),
        )
        assert execution.status is SandboxStatus.COMPLETED
        assert execution.results[0].stdout.startswith("ok")

    def test_stops_after_a_failing_command_unless_failure_is_allowed(self, tmp_path: Path) -> None:
        execution = LocalSubprocessSandbox().run(
            tmp_path,
            [
                CommandSpec(kind=CommandKind.LINT, command='python -c "raise SystemExit(1)"'),
                CommandSpec(kind=CommandKind.TEST, command="python -c \"print('never')\""),
            ],
            limits=SandboxLimits(timeout_seconds=60),
        )
        assert len(execution.results) == 1

    def test_continues_past_a_failure_when_allowed(self, tmp_path: Path) -> None:
        execution = LocalSubprocessSandbox().run(
            tmp_path,
            [
                CommandSpec(
                    kind=CommandKind.LINT,
                    command='python -c "raise SystemExit(1)"',
                    allow_failure=True,
                ),
                CommandSpec(kind=CommandKind.TEST, command="python -c \"print('reached')\""),
            ],
            limits=SandboxLimits(timeout_seconds=60),
        )
        assert len(execution.results) == 2
        assert execution.results[1].passed

    def test_the_host_environment_is_not_visible(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATCHPILOT_SECRET_CANARY", "do-not-leak")
        execution = LocalSubprocessSandbox().run(
            tmp_path,
            [
                CommandSpec(
                    kind=CommandKind.TEST,
                    command=(
                        'python -c "import os; '
                        "print(os.environ.get('PATCHPILOT_SECRET_CANARY', 'absent'))\""
                    ),
                )
            ],
            limits=SandboxLimits(timeout_seconds=60),
        )
        assert "absent" in execution.results[0].stdout
        assert "do-not-leak" not in execution.results[0].stdout

    def test_it_is_refused_in_production(self, tmp_path: Path) -> None:
        sandbox = LocalSubprocessSandbox(environment="production")
        available, reason = sandbox.available()
        assert not available
        assert "no isolation" in reason
        execution = sandbox.run(
            tmp_path,
            [CommandSpec(kind=CommandKind.TEST, command="echo hi")],
            limits=SandboxLimits(),
        )
        assert execution.status is SandboxStatus.UNAVAILABLE

    def test_it_can_be_disabled_explicitly(self, tmp_path: Path) -> None:
        available, reason = LocalSubprocessSandbox(allowed=False).available()
        assert not available
        assert "PATCHPILOT_ALLOW_LOCAL_SANDBOX" in reason

    def test_it_describes_itself_honestly(self) -> None:
        described = LocalSubprocessSandbox().describe()
        assert described["isolation"] == "none"
        assert "host" in str(described["filesystem"])


class TestDockerSandboxConfiguration:
    """Assert the hardening flags without needing a daemon."""

    def test_the_container_arguments_apply_every_documented_control(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sandbox = DockerSandbox("python:3.12-slim-bookworm")
        captured: dict[str, list[str]] = {}

        class Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_docker(*args: str, timeout: int = 0):
            captured["args"] = list(args)
            return Completed()

        monkeypatch.setattr(sandbox, "_docker", fake_docker)
        assert (
            sandbox._start_container("c", SandboxLimits(memory_mb=512, cpus=2, pids=64), 60) is None
        )

        args = captured["args"]
        joined = " ".join(args)
        assert "--network none" in joined
        assert "--memory 512m" in joined
        assert "--memory-swap 512m" in joined  # swap disabled
        assert "--cpus 2" in joined
        assert "--pids-limit 64" in joined
        assert "--cap-drop ALL" in joined
        assert "--security-opt no-new-privileges" in joined
        assert "--tmpfs /tmp:rw,size=64m,mode=1777" in joined
        # The workspace is copied in, never mounted from the host.
        assert "-v" not in args
        assert "--volume" not in args
        assert "/var/run/docker.sock" not in joined

    def test_commands_run_as_a_non_root_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sandbox = DockerSandbox()
        captured: dict[str, list[str]] = {}

        class Completed:
            returncode = 0
            stdout = "ok"
            stderr = ""

        def fake_docker(*args: str, timeout: int = 0):
            captured["args"] = list(args)
            return Completed()

        monkeypatch.setattr(sandbox, "_docker", fake_docker)
        sandbox._exec(
            "c",
            CommandSpec(kind=CommandKind.TEST, command="pytest"),
            SandboxLimits(user="10001:10001"),
            {"PATH": "/usr/bin"},
            (),
        )
        args = captured["args"]
        assert args[0] == "exec"
        assert "--user" in args and "10001:10001" in args

    def test_it_describes_itself(self) -> None:
        described = DockerSandbox().describe()
        assert described["host_mounts"] == []
        assert described["docker_socket_mounted"] is False
        assert described["host_env_forwarded"] is False

    def test_missing_daemon_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr("patchpilot_sandbox.docker_sandbox.shutil.which", lambda _: None)
        sandbox = DockerSandbox()
        available, reason = sandbox.available()
        assert not available
        assert "not on PATH" in reason
        execution = sandbox.run(
            tmp_path, [CommandSpec(kind=CommandKind.TEST, command="pytest")], limits=SandboxLimits()
        )
        assert execution.status is SandboxStatus.UNAVAILABLE
        assert execution.error

    def test_a_cli_that_cannot_reach_its_daemon_is_not_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stopped Docker Desktop: ``docker info`` exits 0 with no server version."""

        class Completed:
            returncode = 0
            stdout = '""\n'
            stderr = "request returned 500 Internal Server Error for API route\n"

        monkeypatch.setattr(
            "patchpilot_sandbox.docker_sandbox.shutil.which", lambda _: "/usr/bin/docker"
        )
        monkeypatch.setattr(
            "patchpilot_sandbox.docker_sandbox.subprocess.run", lambda *a, **k: Completed()
        )
        available, reason = DockerSandbox().available()
        assert not available
        assert "500 Internal Server Error" in reason


@requires_docker
class TestDockerSandboxLive:
    def test_runs_the_fixture_suite_inside_a_container(
        self, fixture_repo: Path, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        copy_snapshot(fixture_repo, workspace)
        execution = DockerSandbox().run(
            workspace,
            [CommandSpec(kind=CommandKind.VALIDATION, command="python -m pytest -q")],
            limits=SandboxLimits(timeout_seconds=300),
        )
        assert execution.status is SandboxStatus.COMPLETED
        assert execution.results[0].exit_code == 1  # the fixture bug is unfixed

    def test_network_access_is_denied(self, fixture_repo: Path, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        copy_snapshot(fixture_repo, workspace)
        execution = DockerSandbox().run(
            workspace,
            [
                CommandSpec(
                    kind=CommandKind.TEST,
                    command=(
                        'python -c "import socket; '
                        "socket.create_connection(('1.1.1.1', 80), timeout=5)\""
                    ),
                )
            ],
            limits=SandboxLimits(network="none", timeout_seconds=60),
        )
        assert execution.results[0].exit_code != 0

    def test_commands_do_not_run_as_root(self, fixture_repo: Path, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        copy_snapshot(fixture_repo, workspace)
        execution = DockerSandbox().run(
            workspace,
            [CommandSpec(kind=CommandKind.TEST, command="id -u")],
            limits=SandboxLimits(timeout_seconds=60),
        )
        assert execution.results[0].stdout.strip() != "0"

    def test_the_host_workspace_is_not_writable_from_inside(
        self, fixture_repo: Path, tmp_path: Path
    ) -> None:
        workspace = tmp_path / "ws"
        copy_snapshot(fixture_repo, workspace)
        marker = "created-inside-the-sandbox.txt"
        DockerSandbox().run(
            workspace,
            [CommandSpec(kind=CommandKind.TEST, command=f"touch {marker}")],
            limits=SandboxLimits(timeout_seconds=60),
        )
        assert not (workspace / marker).exists()


class TestSandboxSelection:
    def test_explicit_local_selection_is_marked_unisolated(self, settings) -> None:
        selection = select_sandbox(settings, backend="local")
        assert selection.backend == "local"
        assert selection.isolated is False
        assert selection.describe()["isolated"] is False

    def test_auto_reports_a_reason_when_nothing_is_usable(self, settings, monkeypatch) -> None:
        monkeypatch.setattr("patchpilot_sandbox.docker_sandbox.shutil.which", lambda _: None)
        settings.sandbox_backend = "auto"
        settings.allow_local_sandbox = False
        selection = select_sandbox(settings, backend="auto")
        assert not selection.available
        assert "no usable sandbox backend" in selection.reason
        # Both failures are explained, not just the last one tried.
        assert "Docker:" in selection.reason and "Local:" in selection.reason

    def test_auto_falls_back_with_an_explanatory_reason(self, settings, monkeypatch) -> None:
        monkeypatch.setattr("patchpilot_sandbox.docker_sandbox.shutil.which", lambda _: None)
        settings.sandbox_backend = "auto"
        selection = select_sandbox(settings, backend="auto")
        assert selection.backend == "local"
        assert selection.isolated is False
        assert "Docker unavailable" in selection.reason
