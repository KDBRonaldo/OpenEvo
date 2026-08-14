#!/usr/bin/env python3
"""Development-only loopback daemon for the minimal Desktop -> real Codex turn.

This is intentionally not the release OpenEvo Daemon and must never be exposed directly to a
network. Bind it to the server loopback interface and reach it only through an SSH tunnel.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


MAX_REQUEST_BYTES = 64 * 1024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 300
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ALLOWED_REQUEST_FIELDS = {
    "schema_version",
    "project_id",
    "project_name",
    "task_title",
    "instruction",
}


class RequestError(ValueError):
    pass


class AgentRunError(RuntimeError):
    pass


def validate_request(payload: object) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise RequestError("request body must be a JSON object")
    unknown = set(payload) - ALLOWED_REQUEST_FIELDS
    if unknown:
        raise RequestError(f"unknown request fields: {', '.join(sorted(unknown))}")
    if payload.get("schema_version") != "1":
        raise RequestError("schema_version must be '1'")

    project_id = payload.get("project_id")
    if not isinstance(project_id, str) or not ID_PATTERN.fullmatch(project_id):
        raise RequestError("project_id is invalid")

    result = {"project_id": project_id}
    for field, maximum in (("project_name", 200), ("task_title", 200), ("instruction", 32_000)):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise RequestError(f"{field} must be a non-empty string")
        if len(value) > maximum:
            raise RequestError(f"{field} is too long")
        result[field] = value.strip()
    return result


def extract_event_logs(stdout: str) -> list[str]:
    messages: list[str] = []
    for raw_line in stdout.splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type not in {"item.completed"}:
            messages.append(f"Codex event: {event_type}")
    return messages[-20:]


class CodexRunner:
    def __init__(self, codex_binary: str, timeout_seconds: int, model: str | None) -> None:
        self._codex_binary = codex_binary
        self._timeout_seconds = timeout_seconds
        self._model = model

    def check_ready(self) -> None:
        try:
            result = subprocess.run(
                [self._codex_binary, "login", "status"],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgentRunError(f"could not check Codex login status: {exc}") from exc
        status_output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.returncode != 0 or "Logged in" not in status_output:
            detail = status_output.strip()[:500]
            raise AgentRunError(f"Codex is not logged in: {detail or 'login status failed'}")

    def run(self, request: dict[str, str]) -> dict[str, Any]:
        prompt = (
            "You are answering one user message inside an OpenEvo development session. "
            "Do not edit files or run shell commands. Return only the helpful answer that should "
            "be shown to the user.\n\n"
            f"Project: {request['project_name']}\n"
            f"Session: {request['task_title']}\n\n"
            f"User message:\n{request['instruction']}\n"
        )
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="openevo-dev-agent-") as temporary_directory:
            workdir = Path(temporary_directory)
            output_path = workdir / "last-message.txt"
            argv = [
                self._codex_binary,
                "exec",
                "--json",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--sandbox",
                "read-only",
                "--disable",
                "shell_tool",
                "--skip-git-repo-check",
                "--cd",
                os.fspath(workdir),
                "--output-last-message",
                os.fspath(output_path),
            ]
            if self._model:
                argv.extend(("--model", self._model))
            argv.append("-")
            try:
                completed = subprocess.run(
                    argv,
                    input=prompt,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                raise AgentRunError(f"Codex exceeded the {self._timeout_seconds}s timeout") from exc
            except OSError as exc:
                raise AgentRunError(f"Codex could not be started: {exc}") from exc

            stdout_bytes = len(completed.stdout.encode("utf-8"))
            stderr_bytes = len(completed.stderr.encode("utf-8"))
            if stdout_bytes + stderr_bytes > MAX_CAPTURE_BYTES:
                raise AgentRunError("Codex process output exceeded the development safety limit")
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout).strip()[-4_000:]
                raise AgentRunError(f"Codex exited with code {completed.returncode}: {detail}")
            try:
                response = output_path.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise AgentRunError(f"Codex did not publish a final response: {exc}") from exc
            if not response:
                raise AgentRunError("Codex published an empty final response")
            if len(response.encode("utf-8")) > MAX_REQUEST_BYTES:
                raise AgentRunError("Codex final response exceeded the development safety limit")

        duration_ms = round((time.monotonic() - started) * 1000)
        return {
            "schema_version": "1",
            "session_id": f"dev-session-{secrets.token_hex(8)}",
            "response": response,
            "model": self._model,
            "duration_ms": duration_ms,
            "logs": [
                "Remote development daemon admitted the session.",
                *extract_event_logs(completed.stdout),
                f"Codex completed the session in {duration_ms} ms.",
            ],
        }


class DevelopmentAgentServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], token: str, runner: CodexRunner) -> None:
        super().__init__(address, DevelopmentAgentHandler)
        self.token = token
        self.runner = runner
        self.turn_lock = threading.Lock()


class DevelopmentAgentHandler(BaseHTTPRequestHandler):
    server: DevelopmentAgentServer
    server_version = "OpenEvoDevelopmentAgent/1"

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if self.path != "/openevo-dev-agent/health":
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
            return
        self._json(HTTPStatus.OK, {"schema_version": "1", "status": "ready"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            return
        if self.path != "/openevo-dev-agent/v1/sessions":
            self._json_error(HTTPStatus.NOT_FOUND, "not_found", "endpoint not found")
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", "Content-Length is invalid")
            return
        if content_length <= 0 or content_length > MAX_REQUEST_BYTES:
            self._json_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large", "request body is too large")
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
            request = validate_request(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, RequestError) as exc:
            self._json_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            return
        if not self.server.turn_lock.acquire(blocking=False):
            self._json_error(HTTPStatus.CONFLICT, "agent_busy", "another development session is running")
            return
        try:
            result = self.server.runner.run(request)
        except AgentRunError as exc:
            self._json_error(HTTPStatus.BAD_GATEWAY, "codex_failed", str(exc))
        else:
            self._json(HTTPStatus.OK, result)
        finally:
            self.server.turn_lock.release()

    def _authorized(self) -> bool:
        expected = f"Bearer {self.server.token}"
        actual = self.headers.get("Authorization", "")
        if not hmac.compare_digest(actual, expected):
            self._json_error(HTTPStatus.UNAUTHORIZED, "unauthorized", "valid bearer token required")
            return False
        return True

    def _json_error(self, status: HTTPStatus, code: str, message: str) -> None:
        self._json(status, {"schema_version": "1", "error": {"code": code, "message": message}})

    def _json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {format % args}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the development-only OpenEvo Codex bridge")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--codex-binary", default="codex")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit("--port must be between 1 and 65535")
    if not 10 <= args.timeout_seconds <= 1_800:
        raise SystemExit("--timeout-seconds must be between 10 and 1800")
    token = os.environ.get("OPENEVO_DEV_AGENT_TOKEN", "").strip()
    if len(token) < 32:
        raise SystemExit("OPENEVO_DEV_AGENT_TOKEN must contain at least 32 characters")
    codex_binary = shutil.which(args.codex_binary)
    if not codex_binary:
        raise SystemExit(f"Codex executable was not found: {args.codex_binary}")
    model = os.environ.get("OPENEVO_DEV_CODEX_MODEL", "").strip() or None
    runner = CodexRunner(codex_binary, args.timeout_seconds, model)
    runner.check_ready()
    server = DevelopmentAgentServer(("127.0.0.1", args.port), token, runner)
    print(f"Development agent daemon listening on 127.0.0.1:{args.port}", flush=True)
    print("It is loopback-only; connect through an SSH local-forward tunnel.", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("Stopping development agent daemon.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
