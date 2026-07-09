# OpenEvo Desktop SSH Transport Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a real, subprocess-backed SSH transport for OpenEvo Desktop sidecar execution while preserving the existing dry-run default and fakeable executor boundary.

**Architecture:** Implement `SshRemoteExecutorTransport` behind the existing `RemoteExecutorTransport` protocol. The transport uses local system `ssh` for remote commands and `rsync` for directory upload, with all subprocess calls built as argv lists and test-injected through a runner. CLI transport selection is explicit: dry-run remains default, `--transport ssh` opts into network access.

**Tech Stack:** Python 3.11+, standard-library `subprocess`, Pydantic v2 models already in `openevo.sidecar`, pytest, argparse CLI, OpenSSH/rsync as external runtime tools.

Tracked by issue #43.

---

## File Structure

- Create `src/openevo/remote/ssh.py`: concrete transport, subprocess runner protocol, argv builders, validation, quoting helpers.
- Modify `src/openevo/remote/__init__.py`: export SSH transport symbols.
- Modify `src/openevo/cli.py`: add `--transport dry-run|ssh` for `openevo sidecar execute` and instantiate the selected transport.
- Create `tests/openevo/remote/test_ssh_transport.py`: unit tests for argv construction, env/cwd quoting, upload behavior, timeout/error mapping, and unsupported auth.
- Modify `tests/openevo/test_cli.py`: CLI tests for default dry-run preservation and explicit SSH transport selection.
- Create `docs/architecture/openevo-desktop-ssh-transport-foundation.md`: supported auth modes, external tool requirements, command semantics, unsupported password vault behavior, verification.

---

### Task 1: SSH Transport Command Execution

**Files:**
- Create: `src/openevo/remote/ssh.py`
- Modify: `src/openevo/remote/__init__.py`
- Test: `tests/openevo/remote/test_ssh_transport.py`

- [ ] **Step 1: Write failing transport tests**

Create tests that define an injected runner and profile helpers:

```python
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openevo.remote import RemoteCommandResult, RemoteExecutorTransport
from openevo.remote.ssh import SshRemoteExecutorTransport
from openevo.sidecar import RemoteProfileConfig


class RecordingRunner:
    def __init__(self, *, fail: bool = False, timeout: bool = False) -> None:
        self.fail = fail
        self.timeout = timeout
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: list[str], timeout_seconds: float):
        self.calls.append((argv, timeout_seconds))
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, timeout_seconds)
        return subprocess.CompletedProcess(
            argv,
            7 if self.fail else 0,
            stdout="out",
            stderr="err",
        )


def _profile(**extra) -> RemoteProfileConfig:
    payload = {
        "version": 1,
        "id": "lab-gpu",
        "host": "gpu.example.edu",
        "port": 2222,
        "user": "alice",
    }
    payload.update(extra)
    return RemoteProfileConfig.model_validate(payload)
```

Add tests for:

```python
def test_ssh_transport_satisfies_executor_protocol() -> None:
    assert isinstance(SshRemoteExecutorTransport(_profile()), RemoteExecutorTransport)


def test_run_invokes_ssh_with_batch_mode_and_maps_result() -> None:
    runner = RecordingRunner()
    transport = SshRemoteExecutorTransport(_profile(), runner=runner)

    result = transport.run("true", timeout_seconds=12.5)

    assert result == RemoteCommandResult(
        command="true",
        return_code=0,
        stdout="out",
        stderr="err",
    )
    assert runner.calls == [
        (
            [
                "ssh",
                "-p",
                "2222",
                "-o",
                "BatchMode=yes",
                "-l",
                "alice",
                "--",
                "gpu.example.edu",
                "true",
            ],
            12.5,
        )
    ]


def test_run_maps_nonzero_exit_without_throwing() -> None:
    runner = RecordingRunner(fail=True)
    transport = SshRemoteExecutorTransport(_profile(), runner=runner)

    result = transport.run("false")

    assert result.return_code == 7
    assert result.stdout == "out"
    assert result.stderr == "err"


def test_private_key_auth_adds_identity_file_as_argv(tmp_path: Path) -> None:
    key = tmp_path / "id_ed25519"
    key.write_text("key", encoding="utf-8")
    runner = RecordingRunner()
    transport = SshRemoteExecutorTransport(
        _profile(
            auth={
                "method": "private_key",
                "private_key_path": str(key),
            }
        ),
        runner=runner,
    )

    transport.run("true")

    argv = runner.calls[0][0]
    assert argv[0:6] == ["ssh", "-p", "2222", "-i", str(key), "-o"]
    assert "BatchMode=yes" in argv
```

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_ssh_transport.py -q
```

Expected: fail because `openevo.remote.ssh` does not exist.

- [ ] **Step 3: Implement minimal command execution transport**

Create `src/openevo/remote/ssh.py`:

```python
from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from openevo.remote.preflight import RemoteCommandResult
from openevo.sidecar import RemoteProfileConfig

CompletedRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


class SshRemoteExecutorTransport:
    def __init__(
        self,
        profile: RemoteProfileConfig,
        *,
        runner: CompletedRunner | None = None,
    ) -> None:
        _validate_supported_auth(profile)
        _validate_remote_identity(profile.user, "user")
        _validate_remote_identity(profile.host, "host")
        self._profile = profile
        self._runner = runner or _run_subprocess

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> RemoteCommandResult:
        remote_command = _remote_command(command, cwd=cwd, env=env)
        try:
            completed = self._runner(
                self._ssh_argv(remote_command),
                timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"SSH command timed out after {timeout_seconds}s") from exc
        return RemoteCommandResult(
            command=command,
            return_code=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def upload_dir(self, local_path: str, remote_path: str) -> None:
        raise NotImplementedError("upload_dir is implemented in Task 3")

    def _ssh_argv(self, remote_command: str) -> list[str]:
        profile = self._profile
        argv = ["ssh", "-p", str(profile.port)]
        if profile.auth.method == "private_key":
            argv.extend(["-i", str(Path(str(profile.auth.private_key_path)).expanduser())])
        argv.extend(["-o", "BatchMode=yes", "-l", profile.user, "--", profile.host, remote_command])
        return argv


def _run_subprocess(argv: list[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
```

Implement helpers:

```python
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REMOTE_HOST_RE = re.compile(r"^[A-Za-z0-9._%-]+$")
_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/@%+=,-]*$")
_REMOTE_USER_RE = re.compile(r"^[A-Za-z0-9._%+-]+$")


def _validate_supported_auth(profile: RemoteProfileConfig) -> None:
    if profile.auth.method == "password_ref":
        raise ValueError("SSH transport does not support password_ref auth yet")
    if profile.auth.passphrase_ref is not None:
        raise ValueError("SSH transport does not support passphrase_ref auth yet")


def _validate_remote_identity(value: str, field_name: str, pattern: re.Pattern[str]) -> None:
    if value.startswith("-") or not pattern.fullmatch(value):
        raise ValueError(f"remote profile {field_name} contains unsupported characters")


def _remote_command(
    command: str,
    *,
    cwd: str | None,
    env: dict[str, str] | None,
) -> str:
    pieces: list[str] = []
    if cwd is not None:
        _validate_remote_absolute_path(cwd, "cwd")
        pieces.append(f"cd {shlex.quote(cwd)}")
    env_prefix = _env_prefix(env or {})
    pieces.append(f"{env_prefix}{command}" if env_prefix else command)
    return " && ".join(pieces)
```

Validation:

```python
def _env_prefix(env: dict[str, str]) -> str:
    assignments: list[str] = []
    for key, value in env.items():
        if not _ENV_KEY_RE.fullmatch(key):
            raise ValueError(f"invalid remote environment key: {key!r}")
        assignments.append(f"{key}={shlex.quote(value)}")
    if not assignments:
        return ""
    return "env " + " ".join(assignments) + " "


def _validate_remote_absolute_path(path: str, field_name: str) -> None:
    if not path.startswith("/"):
        raise ValueError(f"{field_name} must be an absolute remote path")
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        raise ValueError(f"{field_name} must not contain control characters")
    if not _REMOTE_PATH_RE.fullmatch(path):
        raise ValueError(f"{field_name} contains unsupported characters")
```

Export `SshRemoteExecutorTransport` from `src/openevo/remote/__init__.py`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_ssh_transport.py -q
```

Expected: command execution tests pass.

- [ ] **Step 5: Commit Task 1**

```bash
git add src/openevo/remote/ssh.py src/openevo/remote/__init__.py tests/openevo/remote/test_ssh_transport.py
git commit -m "feat: add ssh remote command transport"
```

---

### Task 2: Environment, cwd, and Auth Guardrails

**Files:**
- Modify: `src/openevo/remote/ssh.py`
- Test: `tests/openevo/remote/test_ssh_transport.py`

- [ ] **Step 1: Write failing guardrail tests**

Add tests:

```python
def test_run_quotes_remote_env_values_and_cwd() -> None:
    runner = RecordingRunner()
    transport = SshRemoteExecutorTransport(_profile(), runner=runner)

    transport.run(
        "python script.py",
        cwd="/home/alice/project-dir",
        env={
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "PIP_INDEX_URL": "https://mirror.example/simple path",
        },
    )

    remote_command = runner.calls[0][0][-1]
    assert remote_command == (
        "cd /home/alice/project-dir && "
        "env HTTPS_PROXY=http://127.0.0.1:7890 "
        "PIP_INDEX_URL='https://mirror.example/simple path' "
        "python script.py"
    )


def test_run_rejects_invalid_env_key() -> None:
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="invalid remote environment key"):
        transport.run("true", env={"BAD KEY": "value"})


def test_run_rejects_relative_cwd() -> None:
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="cwd must be an absolute remote path"):
        transport.run("true", cwd="relative")


def test_run_rejects_cwd_with_shell_metacharacter() -> None:
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="cwd contains unsupported characters"):
        transport.run("true", cwd="/tmp/project;touch-pwned")


def test_password_ref_auth_is_not_supported_without_vault() -> None:
    with pytest.raises(ValueError, match="password_ref"):
        SshRemoteExecutorTransport(
            _profile(auth={"method": "password_ref", "password_ref": "secret-id"})
        )


def test_rejects_unsafe_remote_identity() -> None:
    with pytest.raises(ValueError, match="host"):
        SshRemoteExecutorTransport(_profile(host="-oProxyCommand=bad"))


def test_rejects_host_with_at_sign_to_prevent_target_rewrite() -> None:
    with pytest.raises(ValueError, match="host"):
        SshRemoteExecutorTransport(_profile(host="trusted.example@attacker.example"))


def test_rejects_colon_host_until_ipv6_rsync_destinations_are_supported() -> None:
    with pytest.raises(ValueError, match="host"):
        SshRemoteExecutorTransport(_profile(host="2001:db8::1"))
```

- [ ] **Step 2: Verify RED**

Run the new tests:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_ssh_transport.py -q
```

Expected: fail until guardrails are complete.

- [ ] **Step 3: Implement guardrails**

Complete `_remote_command()`, `_env_prefix()`, `_validate_supported_auth()`, `_validate_remote_identity()`, and `_validate_remote_absolute_path()` as specified in Task 1. Keep error messages generic; do not include password or passphrase ref values.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_ssh_transport.py -q
```

Expected: all SSH transport tests pass.

- [ ] **Step 5: Commit Task 2**

```bash
git add src/openevo/remote/ssh.py tests/openevo/remote/test_ssh_transport.py
git commit -m "fix: harden ssh transport command construction"
```

---

### Task 3: Directory Upload via rsync

**Files:**
- Modify: `src/openevo/remote/ssh.py`
- Test: `tests/openevo/remote/test_ssh_transport.py`

- [ ] **Step 1: Write failing upload tests**

Add tests:

```python
def test_upload_dir_creates_remote_parent_and_runs_rsync(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    runner = RecordingRunner()
    transport = SshRemoteExecutorTransport(_profile(), runner=runner)

    transport.upload_dir(str(local), "/home/alice/.openevo/workspaces/task")

    assert runner.calls[0][0][-1] == (
        "mkdir -p /home/alice/.openevo/workspaces/task"
    )
    assert runner.calls[1][0][0:3] == ["rsync", "-az", "--delete"]
    assert runner.calls[1][0][-2] == f"{local}/"
    assert runner.calls[1][0][-1] == (
        "gpu.example.edu:/home/alice/.openevo/workspaces/task/"
    )
    assert "-l alice" in runner.calls[1][0][4]


def test_upload_dir_rejects_missing_local_path(tmp_path: Path) -> None:
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(FileNotFoundError):
        transport.upload_dir(str(tmp_path / "missing"), "/remote/path")


def test_upload_dir_rejects_relative_remote_path(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="remote_path must be an absolute remote path"):
        transport.upload_dir(str(local), "relative/path")


def test_upload_dir_rejects_remote_path_with_shell_metacharacter(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    transport = SshRemoteExecutorTransport(_profile(), runner=RecordingRunner())

    with pytest.raises(ValueError, match="remote_path contains unsupported characters"):
        transport.upload_dir(str(local), "/remote/path;touch-pwned")


def test_upload_dir_raises_when_rsync_fails(tmp_path: Path) -> None:
    local = tmp_path / "workspace"
    local.mkdir()
    runner = RecordingRunner(fail=True)
    transport = SshRemoteExecutorTransport(_profile(), runner=runner)

    with pytest.raises(RuntimeError, match="rsync failed"):
        transport.upload_dir(str(local), "/remote/path")
```

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_ssh_transport.py -q
```

Expected: upload tests fail because `upload_dir()` is not implemented.

- [ ] **Step 3: Implement upload_dir**

Implement:

```python
def upload_dir(self, local_path: str, remote_path: str) -> None:
    local = Path(local_path).expanduser()
    if not local.exists():
        raise FileNotFoundError(f"Local workspace path not found: {local}")
    if not local.is_dir():
        raise ValueError(f"Local workspace path is not a directory: {local}")
    _validate_remote_absolute_path(remote_path, "remote_path")

    mkdir_result = self.run(f"mkdir -p {shlex.quote(remote_path)}")
    if not mkdir_result.ok:
        raise RuntimeError(f"remote mkdir failed: {mkdir_result.stderr}")

    argv = self._rsync_argv(local, remote_path)
    completed = self._runner(argv, 300.0)
    if completed.returncode != 0:
        raise RuntimeError(f"rsync failed: {completed.stderr or completed.stdout}")
```

Add:

```python
def _rsync_argv(self, local: Path, remote_path: str) -> list[str]:
    ssh_command = " ".join(shlex.quote(part) for part in self._ssh_base_argv())
    return [
        "rsync",
        "-az",
        "--delete",
        "-e",
        ssh_command,
        _with_trailing_slash(str(local)),
        f"{self._profile.host}:{_with_trailing_slash(remote_path)}",
    ]
```

Refactor `_ssh_argv()` so `_ssh_base_argv()` returns all SSH args except destination and remote command.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/remote/test_ssh_transport.py -q
```

Expected: all upload and command tests pass.

- [ ] **Step 5: Commit Task 3**

```bash
git add src/openevo/remote/ssh.py tests/openevo/remote/test_ssh_transport.py
git commit -m "feat: upload sidecar workspaces over rsync"
```

---

### Task 4: CLI Transport Selection

**Files:**
- Modify: `src/openevo/cli.py`
- Modify: `tests/openevo/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Add tests that monkeypatch `openevo.cli.SshRemoteExecutorTransport`:

```python
class _CliRecordingSshTransport:
    profiles = []

    def __init__(self, profile):
        self.profiles.append(profile)

    def run(self, command, *, cwd=None, env=None, timeout_seconds=30.0):
        if command == 'df -Pk "$HOME"':
            return RemoteCommandResult(
                command=command,
                return_code=0,
                stdout=(
                    "Filesystem 1024-blocks Used Available Capacity Mounted on\n"
                    "/dev/root 100000000 1 99999999 1% /home\n"
                ),
            )
        return RemoteCommandResult(command=command, return_code=0, stdout="ok")

    def upload_dir(self, local_path, remote_path):
        return None
```

Test:

```python
def test_cli_sidecar_execute_transport_ssh_uses_ssh_transport(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    _CliRecordingSshTransport.profiles = []
    monkeypatch.setattr("openevo.cli.SshRemoteExecutorTransport", _CliRecordingSshTransport)
    science_path = _write_config(tmp_path / "science.yaml", _minimal_science_payload())
    profile_path = _write_config(
        tmp_path / "remote.yaml",
        {
            "version": 1,
            "id": "science-team",
            "host": "gpu.example.edu",
            "user": "alice",
        },
    )

    exit_code = main(
        [
            "sidecar",
            "execute",
            str(science_path),
            "--remote-profile",
            str(profile_path),
            "--transport",
            "ssh",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert _CliRecordingSshTransport.profiles[0].id == "science-team"
```

Update the existing default preflight test to assert default dry-run still works without `--transport`.

- [ ] **Step 2: Verify RED**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py::test_cli_sidecar_execute_transport_ssh_uses_ssh_transport -q
```

Expected: fail because `--transport` does not exist.

- [ ] **Step 3: Implement CLI selection**

Modify parser:

```python
execute_parser.add_argument(
    "--transport",
    choices=("dry-run", "ssh"),
    default="dry-run",
    help="Remote executor transport to use. Defaults to dry-run.",
)
```

Import `SshRemoteExecutorTransport` from `openevo.remote`.

Add helper:

```python
def _sidecar_transport(args: argparse.Namespace, profile):
    if args.transport == "dry-run":
        return _CliDryRunTransport()
    if args.transport == "ssh":
        return SshRemoteExecutorTransport(profile)
    raise ValueError(f"Unknown sidecar transport: {args.transport}")
```

Use it in `_handle_sidecar_execute()`.

- [ ] **Step 4: Verify GREEN**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py -q
```

Expected: all CLI tests pass.

- [ ] **Step 5: Commit Task 4**

```bash
git add src/openevo/cli.py tests/openevo/test_cli.py
git commit -m "feat: select ssh sidecar transport from cli"
```

---

### Task 5: Documentation and Final Verification

**Files:**
- Create: `docs/architecture/openevo-desktop-ssh-transport-foundation.md`
- Modify: `docs/architecture/openevo-desktop-remote-executor-foundation.md`

- [ ] **Step 1: Write docs**

Create architecture docs covering:

- `--transport dry-run` remains default and never opens network connections.
- `--transport ssh` requires local OpenSSH and rsync.
- Supported auth modes: `ssh_agent` and `private_key`.
- Unsupported auth modes: `password_ref` and passphrase refs until Desktop has a vault.
- Remote command env variables are injected for the remote command, not SSH connection proxying.
- Upload uses rsync and requires local source directories.
- SSH transport remote paths are restricted to the safe path character set
  `/`, letters, digits, `.`, `_`, `-`, `@`, `%`, `+`, `=`, `,`.
- Known limitations: no remote daemon, no dependency auto-repair, no Docker/vLLM lifecycle, no UI wiring.

Update the previous remote executor doc to point to the SSH transport doc as the first concrete transport.

- [ ] **Step 2: Run final verification**

Run:

```bash
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/test_cli.py tests/openevo/sidecar tests/openevo/remote tests/openevo/science tests/evolution/test_models.py -q
PYTHONPATH=src /home/ziyi/ProRL-Agent-Server/.venv/bin/python -m pytest tests/openevo/science tests/evolution/test_models.py --collect-only -q >/tmp/openevo-ssh-transport-collect.txt
/home/ziyi/ProRL-Agent-Server/.venv/bin/ruff check src/openevo/remote src/openevo/cli.py tests/openevo/remote tests/openevo/test_cli.py
git diff --check openevo/stable...HEAD
```

Expected: tests and checks pass.

- [ ] **Step 3: Commit docs**

```bash
git add docs/architecture/openevo-desktop-ssh-transport-foundation.md docs/architecture/openevo-desktop-remote-executor-foundation.md
git commit -m "docs: document ssh sidecar transport"
```

- [ ] **Step 4: Final review**

Dispatch a fresh `gpt-5.5` high-effort reviewer to inspect `openevo/stable...HEAD` for security, command construction, CLI, docs, and test coverage.

- [ ] **Step 5: Publish**

Push branch, open PR against `stable`, include `Fixes #43`, test evidence, docs list, and merge when GitHub checks are clean.
