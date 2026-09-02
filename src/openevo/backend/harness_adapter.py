"""Harness-facing execution adapters used by the OpenEvo daemon boundary.

The daemon owns session orchestration.  A harness adapter owns only the mechanics needed to
prepare and launch one concrete agent harness and to collect its transcript/result.  Keeping
that split makes it possible to add Claude Code or another harness without branching the
session lifecycle on harness names.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
import mimetypes
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any, Protocol


MAX_HARNESS_IMAGE_ATTACHMENTS = 8
MAX_HARNESS_IMAGE_FILE_BYTES = 10 * 1024 * 1024
MAX_HARNESS_IMAGE_BYTES = 32 * 1024 * 1024
HARNESS_IMAGE_MEDIA_TYPES = {"image/gif", "image/jpeg", "image/png", "image/webp"}


class HarnessRunError(RuntimeError):
    """A harness could not produce a valid run result."""


class HarnessRunCancelled(HarnessRunError):
    """The caller cancelled a running harness process."""


class RuntimeContextMaterializer(Protocol):
    def materialize(
        self,
        *,
        persistent_workspace: Path,
        runtime_workspace: Path,
        contexts: object,
    ) -> dict[str, Any]: ...


class RuntimeControlAdapter(Protocol):
    @property
    def capabilities(self) -> Any: ...

    def reconcile(self, intents: object) -> Any: ...


class HarnessAdapter(Protocol):
    """Stable daemon boundary for a concrete agent harness."""

    @property
    def harness_id(self) -> str: ...

    def check_ready(self) -> None: ...

    def runtime_capabilities(self) -> dict[str, Any]: ...

    def run(
        self,
        request: dict[str, Any],
        *,
        cancellation: "HarnessCancellation | None" = None,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]: ...


class HarnessCancellation:
    """Thread-safe cancellation handle that also terminates a bound child process."""

    def __init__(self) -> None:
        self._requested = threading.Event()
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    def raise_if_requested(self) -> None:
        if self.requested:
            raise HarnessRunCancelled("Session cancelled by user")

    def bind(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            self._process = process
            should_terminate = self.requested
        if should_terminate:
            self._terminate(process)

    def release(self, process: subprocess.Popen[str]) -> None:
        with self._lock:
            if self._process is process:
                self._process = None

    def cancel(self) -> None:
        self._requested.set()
        with self._lock:
            process = self._process
        if process is not None:
            self._terminate(process)

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (OSError, ProcessLookupError):
            return


class CodexHarnessAdapter:
    """Codex CLI implementation of the generic harness adapter contract."""

    def __init__(
        self,
        *,
        codex_binary: str,
        timeout_seconds: int,
        model: str | None,
        context_materializer_factory: Callable[[], RuntimeContextMaterializer],
        runtime_control_adapter: RuntimeControlAdapter,
        extract_event_logs: Callable[[str], list[str]],
        max_capture_bytes: int,
        max_response_bytes: int,
        max_workspace_context_bytes: int,
    ) -> None:
        self._codex_binary = codex_binary
        self._timeout_seconds = timeout_seconds
        self._model = model
        self._context_materializer_factory = context_materializer_factory
        self._context_materializer: RuntimeContextMaterializer | None = None
        self._runtime_adapter = runtime_control_adapter
        self._extract_event_logs = extract_event_logs
        self._max_capture_bytes = max_capture_bytes
        self._max_response_bytes = max_response_bytes
        self._max_workspace_context_bytes = max_workspace_context_bytes

    @property
    def harness_id(self) -> str:
        return "codex"

    @property
    def codex_binary(self) -> str:
        return self._codex_binary

    @property
    def model(self) -> str | None:
        return self._model

    def runtime_capabilities(self) -> dict[str, Any]:
        return {"schema_version": "1", **self._runtime_adapter.capabilities.to_dict()}

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
            raise HarnessRunError(f"could not check Codex login status: {exc}") from exc
        status_output = "\n".join(part for part in (result.stdout, result.stderr) if part)
        if result.returncode != 0 or "Logged in" not in status_output:
            detail = status_output.strip()[:500]
            raise HarnessRunError(f"Codex is not logged in: {detail or 'login status failed'}")

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _workspace_context(self, snapshot: object) -> str:
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("entries"), list):
            return "[]"
        projected: list[dict[str, Any]] = []
        consumed = 2
        for entry in snapshot["entries"]:
            if not isinstance(entry, dict):
                continue
            item = {
                "path": entry.get("path"),
                "kind": entry.get("kind"),
                "byte_size": entry.get("byte_size"),
                "media_type": entry.get("media_type"),
                "content": entry.get("content"),
            }
            encoded = self._canonical_json(item).encode("utf-8")
            if consumed + len(encoded) > self._max_workspace_context_bytes:
                break
            projected.append(item)
            consumed += len(encoded) + 1
        return self._canonical_json(projected)

    @staticmethod
    def _parse_workspace_plan(raw_response: str) -> dict[str, Any]:
        try:
            plan = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise HarnessRunError("Codex returned an invalid structured workspace response") from exc
        if not isinstance(plan, dict) or set(plan) != {"answer", "file_writes", "delete_paths"}:
            raise HarnessRunError("Codex returned an invalid structured workspace response")
        answer = plan.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            raise HarnessRunError("Codex returned an empty answer")
        mutations = {"file_writes": plan.get("file_writes"), "delete_paths": plan.get("delete_paths")}
        if not isinstance(mutations["file_writes"], list) or not isinstance(
            mutations["delete_paths"], list
        ):
            raise HarnessRunError("Codex returned an invalid workspace mutation plan")
        return {"answer": answer.strip(), "mutations": mutations}

    def prepare_runtime(self, request: Mapping[str, Any], temporary_root: Path) -> dict[str, Any]:
        workspace_path = request.get("workspace_path")
        if not isinstance(workspace_path, Path) or not workspace_path.is_dir():
            raise HarnessRunError("persistent project workspace is unavailable")
        if self._context_materializer is None:
            try:
                self._context_materializer = self._context_materializer_factory()
            except (ImportError, ModuleNotFoundError) as exc:
                raise HarnessRunError(
                    "OpenEvo runtime projection is unavailable; run this daemon with `uv run python`"
                ) from exc
        return self._context_materializer.materialize(
            persistent_workspace=workspace_path,
            runtime_workspace=temporary_root / "workspace",
            contexts=request.get("evolved_contexts", []),
        )

    @staticmethod
    def inject_memory(runtime_context: Mapping[str, Any]) -> str:
        sections = runtime_context.get("instruction_sections", [])
        memory_sections = "\n\n".join(str(section) for section in sections)
        if not memory_sections:
            return ""
        return (
            "Runtime instructions resolved by OpenEvo Core for this session:\n"
            f"{memory_sections}\n\n"
        )

    @staticmethod
    def install_skills(runtime_context: Mapping[str, Any]) -> list[str]:
        return [str(item) for item in runtime_context.get("activations", []) if "skill" in str(item).lower()]

    @staticmethod
    def apply_agent_system(runtime_context: Mapping[str, Any]) -> list[str]:
        return [str(item) for item in runtime_context.get("activations", []) if "agent" in str(item).lower()]

    def spawn_agents(self, runtime_context: Mapping[str, Any]) -> Any:
        return self._runtime_adapter.reconcile(runtime_context.get("runtime_controls", []))

    def build_command(
        self,
        *,
        runtime_context: Mapping[str, Any],
        schema_path: Path,
        output_path: Path,
        image_paths: list[Path] | None = None,
        inference: Mapping[str, str] | None = None,
    ) -> list[str]:
        argv = [
            self._codex_binary,
            "exec",
            "--json",
            "--ignore-user-config",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--disable",
            "shell_tool",
            "--skip-git-repo-check",
            "--cd",
            os.fspath(runtime_context["workspace_path"]),
            "--output-schema",
            os.fspath(schema_path),
            "--output-last-message",
            os.fspath(output_path),
        ]
        for image_path in image_paths or []:
            argv.extend(("--image", os.fspath(image_path)))
        effective_model = self._model
        if inference is not None:
            effective_model = inference["model"]
            provider_id = "openevo_self_deployed"
            argv.extend((
                "-c", f'model_provider="{provider_id}"',
                "-c", f'model_providers.{provider_id}.name="OpenEvo Self-Deployed"',
                "-c", f'model_providers.{provider_id}.base_url={json.dumps(inference["base_url"])}',
                "-c", f'model_providers.{provider_id}.env_key="OPENAI_API_KEY"',
                "-c", f'model_providers.{provider_id}.wire_api="responses"',
            ))
        if effective_model:
            argv.extend(("--model", effective_model))
        argv.append("-")
        return argv

    @staticmethod
    def _image_attachments(runtime_context: Mapping[str, Any]) -> list[Path]:
        workspace_path = runtime_context.get("workspace_path")
        if not isinstance(workspace_path, Path) or not workspace_path.is_dir():
            return []
        attachments: list[Path] = []
        consumed = 0
        for path in sorted(workspace_path.rglob("*")):
            try:
                relative = path.relative_to(workspace_path)
                if relative.parts and relative.parts[0] in {".agents", ".openevo"}:
                    continue
                if path.is_symlink() or not path.is_file():
                    continue
                media_type = mimetypes.guess_type(path.name)[0]
                size = path.stat().st_size
            except OSError:
                continue
            if media_type not in HARNESS_IMAGE_MEDIA_TYPES:
                continue
            if size > MAX_HARNESS_IMAGE_FILE_BYTES or consumed + size > MAX_HARNESS_IMAGE_BYTES:
                continue
            attachments.append(path)
            consumed += size
            if len(attachments) >= MAX_HARNESS_IMAGE_ATTACHMENTS:
                break
        return attachments

    def collect_transcript(
        self,
        *,
        process: subprocess.Popen[str],
        stdout: str,
        stderr: str,
        output_path: Path,
    ) -> tuple[dict[str, Any], list[str]]:
        if len(stdout.encode("utf-8")) + len(stderr.encode("utf-8")) > self._max_capture_bytes:
            raise HarnessRunError("Codex process output exceeded the development safety limit")
        if process.returncode != 0:
            detail = (stderr or stdout).strip()[-4_000:]
            raise HarnessRunError(f"Codex exited with code {process.returncode}: {detail}")
        try:
            raw_response = output_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise HarnessRunError(f"Codex did not publish a final response: {exc}") from exc
        if not raw_response:
            raise HarnessRunError("Codex published an empty final response")
        if len(raw_response.encode("utf-8")) > self._max_response_bytes:
            raise HarnessRunError("Codex final response exceeded the development safety limit")
        return self._parse_workspace_plan(raw_response), self._extract_event_logs(stdout)

    def run(
        self,
        request: dict[str, Any],
        *,
        cancellation: HarnessCancellation | None = None,
        log: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        cancellable_process = cancellation is not None
        cancellation = cancellation or HarnessCancellation()
        emit = log or (lambda _message: None)
        cancellation.raise_if_requested()
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="openevo-dev-agent-") as temporary_directory:
            temporary_root = Path(temporary_directory)
            emit("Preparing the Codex runtime workspace.")
            runtime_context = self.prepare_runtime(request, temporary_root)
            cancellation.raise_if_requested()
            try:
                runtime_activation = self.spawn_agents(runtime_context)
            except Exception as exc:
                raise HarnessRunError(
                    f"Core runtime control is not supported by this Daemon: {exc}"
                ) from exc
            memory_sections = self.inject_memory(runtime_context)
            self.install_skills(runtime_context)
            self.apply_agent_system(runtime_context)
            workspace_context = self._workspace_context(request.get("workspace_snapshot"))
            image_paths = self._image_attachments(runtime_context)
            prompt = (
                "You are planning changes for a persistent OpenEvo project workspace. "
                "The trusted daemon, not you, applies file mutations after validating them. "
                "Do not call shell, patch, or filesystem tools. The supplied workspace JSON is a "
                "bounded index containing text and daemon-extracted document projections. Attached "
                "workspace images are available as visual inputs. Solve the user's task and return "
                "only the requested structured result. "
                "Use relative POSIX paths. Put every complete UTF-8 text file that must be created or "
                "changed in file_writes. Put only regular files that must be removed in delete_paths. "
                "Do not include unchanged files and do not use absolute paths or '..'. "
                "Do not return OpenEvo runtime files under .openevo or injected skills under "
                ".agents/skills as workspace mutations. If no file change is needed, return empty arrays.\n\n"
                f"Project: {request['project_name']}\n"
                f"Session: {request['task_title']}\n\n"
                f"{memory_sections}Current workspace JSON:\n{workspace_context}\n\n"
                f"User message:\n{request['instruction']}\n"
            )
            output_path = temporary_root / "last-message.txt"
            schema_path = temporary_root / "workspace-response.schema.json"
            schema_path.write_text(
                self._canonical_json({
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "answer": {"type": "string"},
                        "file_writes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                                "required": ["path", "content"],
                            },
                        },
                        "delete_paths": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["answer", "file_writes", "delete_paths"],
                }),
                encoding="utf-8",
            )
            argv = self.build_command(
                runtime_context=runtime_context,
                schema_path=schema_path,
                output_path=output_path,
                image_paths=image_paths,
                inference=request.get("inference"),
            )
            environment = os.environ.copy()
            environment.update(runtime_context["environment"])
            inference = request.get("inference")
            if isinstance(inference, Mapping):
                environment["OPENAI_API_KEY"] = "openevo-local"
            emit("Starting the Codex harness process.")
            if not cancellable_process:
                try:
                    completed = subprocess.run(
                        argv,
                        input=prompt,
                        env=environment,
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=self._timeout_seconds,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise HarnessRunError(
                        f"Codex exceeded the {self._timeout_seconds}s timeout"
                    ) from exc
                except OSError as exc:
                    raise HarnessRunError(f"Codex could not be started: {exc}") from exc
                plan, event_logs = self.collect_transcript(
                    process=completed,  # type: ignore[arg-type]
                    stdout=completed.stdout,
                    stderr=completed.stderr,
                    output_path=output_path,
                )
                process = None
            else:
                process = self._run_cancellable_process(
                    argv=argv,
                    prompt=prompt,
                    environment=environment,
                    cancellation=cancellation,
                )
                stdout, stderr = process[1], process[2]
                plan, event_logs = self.collect_transcript(
                    process=process[0],
                    stdout=stdout,
                    stderr=stderr,
                    output_path=output_path,
                )
        duration_ms = round((time.monotonic() - started) * 1000)
        return {
            "schema_version": "1",
            "response": plan["answer"],
            "file_mutations": plan["mutations"],
            "model": (
                request["inference"]["model"]
                if isinstance(request.get("inference"), Mapping)
                else self._model
            ),
            "duration_ms": duration_ms,
            "runtime_activation": runtime_activation.to_dict(),
            "logs": [
                "Remote development daemon admitted the session.",
                *[f"Runtime context: {item}." for item in runtime_context["activations"]],
                *[
                    "Runtime policy: "
                    f"{decision.intent.feature_id} is {decision.status.value} "
                    f"(owner: {decision.owner})."
                    for decision in runtime_activation.decisions
                ],
                *event_logs,
                f"Codex completed the session in {duration_ms} ms.",
            ],
        }

    def _run_cancellable_process(
        self,
        *,
        argv: list[str],
        prompt: str,
        environment: Mapping[str, str],
        cancellation: HarnessCancellation,
    ) -> tuple[subprocess.Popen[str], str, str]:
        cancellation.raise_if_requested()
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
            )
        except OSError as exc:
            raise HarnessRunError(f"Codex could not be started: {exc}") from exc
        cancellation.bind(process)
        try:
            try:
                stdout, stderr = process.communicate(prompt, timeout=self._timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                HarnessCancellation._terminate(process)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise HarnessRunError(
                    f"Codex exceeded the {self._timeout_seconds}s timeout"
                ) from exc
        finally:
            cancellation.release(process)
        cancellation.raise_if_requested()
        return process, stdout, stderr
