from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from desktop.sidecar.lifecycle_logs_v2 import LifecycleOutputSanitizerV2
from desktop.sidecar import system_ssh_session as ssh_session


def test_sanitizer_handles_chunk_boundaries_controls_and_private_authority(
    tmp_path: Path,
) -> None:
    entries: list[tuple[str, str, bool]] = []
    private_path = str(tmp_path / "private" / "daemon")
    sanitizer = LifecycleOutputSanitizerV2(
        lambda source, text, truncated: entries.append((source, text, truncated)),
        secret_canaries=("askpass-capability-secret", "split-bearer-secret"),
        forbidden_endpoints=("http://127.0.0.1:43117",),
        forbidden_paths=(private_path,),
    )

    chunks = (
        b"\x1b[31mstarting\x1b[0m\rprogress 20%\x00\nAuthoriza",
        b"tion: Bearer split-bearer-secret\nproxy=https://alice:password@proxy.test\n",
        f"endpoint=http://127.0.0.1:43117/v1/status path={private_path}\n".encode(),
        b"askpass-capability-",
        b"secret invalid=\xff\n",
    )
    for chunk in chunks:
        sanitizer.feed("ssh_stderr", chunk)
    sanitizer.flush()

    rendered = "".join(text for _source, text, _truncated in entries)
    assert "starting" in rendered and "progress 20%" in rendered
    assert "\x1b" not in rendered and "\x00" not in rendered
    assert "split-bearer-secret" not in rendered
    assert "askpass-capability-secret" not in rendered
    assert "alice:password" not in rendered
    assert "127.0.0.1:43117" not in rendered
    assert private_path not in rendered
    assert "[REDACTED" in rendered
    assert "\ufffd" in rendered
    assert {source for source, _text, _truncated in entries} == {"ssh_stderr"}


def test_sanitizer_splits_utf8_at_exact_public_entry_boundary() -> None:
    entries: list[tuple[str, str, bool]] = []
    sanitizer = LifecycleOutputSanitizerV2(
        lambda source, text, truncated: entries.append((source, text, truncated))
    )

    sanitizer.feed("daemon_stdout", ("界" * 6_000 + "\n").encode())
    sanitizer.flush()

    assert len(entries) == 2
    assert all(len(text.encode("utf-8")) <= 16 * 1024 for _source, text, _ in entries)
    assert all(truncated for _source, _text, truncated in entries)
    assert "".join(text for _source, text, _truncated in entries) == "界" * 6_000 + "\n"


def test_sanitizer_bounds_and_discards_an_unterminated_process_line() -> None:
    entries: list[tuple[str, str, bool]] = []
    sanitizer = LifecycleOutputSanitizerV2(
        lambda source, text, truncated: entries.append((source, text, truncated))
    )

    for _ in range(1_024):
        sanitizer.feed("daemon_stderr", b"x" * 8_192)
    sanitizer.feed("daemon_stderr", b"\nafter oversized output\n")
    sanitizer.flush()

    rendered = "".join(text for _source, text, _truncated in entries)
    assert "x" * 1_024 not in rendered
    assert "unterminated process output omitted" in rendered
    assert "after oversized output\n" in rendered
    assert len(rendered.encode("utf-8")) <= 2 * 16 * 1_024
    assert any(truncated for _source, _text, truncated in entries)


def test_sanitizer_redacts_named_credentials_and_sensitive_query_values() -> None:
    entries: list[tuple[str, str, bool]] = []
    sanitizer = LifecycleOutputSanitizerV2(
        lambda source, text, truncated: entries.append((source, text, truncated))
    )

    sanitizer.feed("ssh_stderr", b"OPENAI_API_")
    sanitizer.feed("ssh_stderr", b"KEY=sk-live-example-value\n")
    sanitizer.feed(
        "daemon_stdout",
        b"request=https://example.test/run?access_token=query-secret&mode=fast\n",
    )
    sanitizer.flush()

    rendered = "".join(text for _source, text, _truncated in entries)
    assert "sk-live-example-value" not in rendered
    assert "query-secret" not in rendered
    assert "OPENAI_API_KEY=[REDACTED_CREDENTIAL]" in rendered
    assert "access_token=[REDACTED_CREDENTIAL]" in rendered


def test_sanitizer_redacts_multiline_secret_canaries_before_line_persistence() -> None:
    entries: list[tuple[str, str, bool]] = []
    sanitizer = LifecycleOutputSanitizerV2(
        lambda source, text, truncated: entries.append((source, text, truncated)),
        secret_canaries=("BEGIN-PRIVATE\nKEY-MATERIAL",),
    )

    sanitizer.feed("ssh_stdout", b"BEGIN-PRIVATE\n")
    sanitizer.feed("ssh_stdout", b"KEY-MATERIAL\nvisible\n")
    sanitizer.flush()

    rendered = "".join(text for _source, text, _truncated in entries)
    assert "BEGIN-PRIVATE" not in rendered
    assert "KEY-MATERIAL" not in rendered
    assert rendered.count("[REDACTED_SECRET]") == 2
    assert "visible\n" in rendered


def test_sanitizer_redacts_arbitrary_absolute_posix_paths() -> None:
    entries: list[tuple[str, str, bool]] = []
    sanitizer = LifecycleOutputSanitizerV2(
        lambda source, text, truncated: entries.append((source, text, truncated))
    )

    sanitizer.feed(
        "daemon_stderr",
        (
            "binary=/usr/local/bin/openevo "
            "app=/Applications/OpenEvo.app/Contents/MacOS/OpenEvo "
            "library=/Library/ApplicationSupport/OpenEvo "
            "device=/dev/null process=/proc/123/status\n"
            "ratio=1/2 url=https://example.test/public/path\n"
        ).encode(),
    )
    sanitizer.flush()

    rendered = "".join(text for _source, text, _truncated in entries)
    for absolute_path in (
        "/usr/local/bin/openevo",
        "/Applications/OpenEvo.app/Contents/MacOS/OpenEvo",
        "/Library/ApplicationSupport/OpenEvo",
        "/dev/null",
        "/proc/123/status",
    ):
        assert absolute_path not in rendered
    assert rendered.count("[REDACTED_HOST_PATH]") == 5
    assert "ratio=1/2" in rendered
    assert "https://example.test/public/path" in rendered


def test_sanitizer_redacts_home_relative_host_paths() -> None:
    entries: list[tuple[str, str, bool]] = []
    sanitizer = LifecycleOutputSanitizerV2(
        lambda source, text, truncated: entries.append((source, text, truncated))
    )

    sanitizer.feed(
        "ssh_stderr",
        (
            "identity=~/.ssh/openevo_ed25519 "
            "other=~researcher/.config/openevo/settings.json "
            "env=$HOME/.ssh/config braced=${HOME}/.ssh/known_hosts\n"
            "roots=~/ ~researcher/ $HOME ${HOME}/\n"
        ).encode(),
    )
    sanitizer.flush()

    rendered = "".join(text for _source, text, _truncated in entries)
    for host_path in (
        "~/.ssh/openevo_ed25519",
        "~researcher/.config/openevo/settings.json",
        "$HOME/.ssh/config",
        "${HOME}/.ssh/known_hosts",
        "~/",
        "~researcher/",
        "$HOME",
        "${HOME}/",
    ):
        assert host_path not in rendered
    assert rendered.count("[REDACTED_HOST_PATH]") == 8


def test_bounded_subprocess_observer_receives_only_stream_and_bytes() -> None:
    observed: list[tuple[str, bytes]] = []
    command_canary = "argv-must-not-be-observed"
    environment_canary = "environment-must-not-be-observed"
    environment = dict(os.environ)
    environment["OPENEVO_TEST_CANARY"] = environment_canary

    completed = ssh_session._run_bounded_subprocess(
        [
            sys.executable,
            "-c",
            "import sys; print('stdout-visible'); print('stderr-visible', file=sys.stderr)",
            command_canary,
        ],
        environment,
        5.0,
        output_observer=lambda source, chunk: observed.append((source, chunk)),
    )

    assert completed.returncode == 0
    assert {source for source, _chunk in observed} == {"ssh_stdout", "ssh_stderr"}
    joined = b"".join(chunk for _source, chunk in observed)
    assert b"stdout-visible" in joined and b"stderr-visible" in joined
    assert command_canary.encode() not in joined
    assert environment_canary.encode() not in joined


def test_output_observer_failure_does_not_change_process_result() -> None:
    def fail_observer(_source: str, _chunk: bytes) -> None:
        raise RuntimeError("log sink unavailable")

    completed = ssh_session._run_bounded_subprocess(
        [sys.executable, "-c", "print('still-successful')"],
        dict(os.environ),
        5.0,
        output_observer=fail_observer,
    )

    assert completed.returncode == 0
    assert completed.stdout == b"still-successful\n"


def test_follower_observer_can_classify_daemon_and_suppress_secret_stdout() -> None:
    observed: list[tuple[str, bytes]] = []
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; print('secret-receipt'); print('daemon-log', file=sys.stderr)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    completed = ssh_session._collect_follower_process(
        process,
        [sys.executable],
        5.0,
        cancel_event=None,
        output_observer=lambda source, chunk: observed.append((source, chunk)),
        stdout_source=None,
        stderr_source="daemon_stderr",
    )

    assert completed.stdout == b"secret-receipt\n"
    assert completed.stderr == b"daemon-log\n"
    assert observed == [("daemon_stderr", b"daemon-log\n")]
